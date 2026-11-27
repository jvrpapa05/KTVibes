"""karaoke-server: lyric videos as a last resort, with the words on screen instead of native lyrics.

When search finds no version whose synced lyrics line up, it offers lyric videos (the artist's official ones first,
fan uploads last) (user, 2026-11-27: "if the video is confirmed to me 'Lyric video' then still show it ... The lyrics
is coming from the video itself, this is the last resort"). The title alone doesn't prove it, so when such a song is
prepared, its video is checked: text recognition (RapidOCR, on the CPU) reads one frame every STEP seconds, text that
stays on screen (a title, a logo, an album cover) is ignored, and the song passes only if changing text is on screen
for most of the time the lead vocals sing, with enough different lines. A song that fails is not played ("if check
fails, do not") and the verdict is kept in meta.json, so search doesn't offer that video again.

Tuned on 2026-11-27 (one frame every 3 s): an official lyric video 80% of sung frames with changing text, 92 lines; a fan
"(Lyrics)" upload 74%, 16 lines; an audio upload (cover picture with the title) 0%, 0; an official music video 0%, 0.
"""
import collections
import re
import subprocess
import threading
from pathlib import Path

STEP = 3.0  # seconds between frames read
SIZE = (960, 540)  # frames are scaled (letterboxed) to this before reading
MIN_CONFIDENCE = 0.6  # a recognised line counts from this confidence
STATIC_SHARE = 0.4  # text in at least this share of all frames is a title or logo, not lyrics
MIN_COVER = 0.5  # changing text in at least half of the frames where the lead vocals sing
MIN_LINES = 8  # and at least this many different lines
VOICE_WINDOW = 1.5  # seconds around a frame in which the lead vocals are measured
VOICE_SHARE = 0.1  # a frame counts as sung when the vocals there reach this share of their loud level

_engine = None
_lock = threading.Lock()

class NoLyricsOnScreen(Exception):
    """The video shows no lyrics; the song is not played."""

MESSAGE = "This video has no lyrics on screen, so it can't be sung: pick another version"

def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower()).strip()

def verdict(samples: list[list[tuple[str, float]]], sung: list[bool] | None = None) -> dict:
    """samples: the recognised (text, confidence) lines of each frame, in order; sung: whether the lead vocals sing
    at each frame (None: every frame counts)."""
    texts = [{_norm(t) for t, c in lines if c >= MIN_CONFIDENCE and len(_norm(t)) >= 3} for lines in samples]
    n = len(texts)
    seen = collections.Counter(t for frame in texts for t in frame)
    static = {t for t, k in seen.items() if n and k >= STATIC_SHARE * n}
    moving = [frame - static for frame in texts]
    counted = [m for m, s in zip(moving, sung if sung and any(sung) else [True] * n) if s]
    cover = sum(1 for m in counted if m) / len(counted) if counted else 0.0
    lines = len({t for m in moving for t in m})
    return {"ok": cover >= MIN_COVER and lines >= MIN_LINES, "cover": round(cover, 2), "lines": lines,
            "frames": n, "sung_frames": len(counted)}

def _frames(video: Path):
    import numpy as np
    w, h = SIZE
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-an", "-vf",
                          f"fps=1/{STEP},scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)

def _sung(vocals: Path | None, count: int) -> list[bool] | None:
    """Whether the lead vocals sing around each frame, from the separated vocals."""
    if not vocals or not vocals.is_file():
        return None
    import numpy as np
    import soundfile as sf
    audio, rate = sf.read(str(vocals), dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    level = []
    for i in range(count):
        t = i * STEP + STEP / 2
        part = mono[max(0, int((t - VOICE_WINDOW) * rate)): int((t + VOICE_WINDOW) * rate)]
        level.append(float(np.sqrt(np.mean(part ** 2))) if len(part) else 0.0)
    loud = float(np.percentile(level, 95)) if level else 0.0
    return [loud > 0 and v >= VOICE_SHARE * loud for v in level]

def check(video: Path, vocals: Path | None = None) -> dict:
    """Read the video's text; returns verdict() with the seconds it took."""
    import time
    global _engine
    started = time.monotonic()
    frames = _frames(video)
    with _lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
        samples = []
        for frame in frames:
            found, _ = _engine(frame)
            samples.append([(r[1], float(r[2])) for r in (found or [])])
    result = verdict(samples, _sung(vocals, len(samples)))
    result["seconds"] = round(time.monotonic() - started, 1)
    return result

def is_official(result: dict, artist: str = "") -> bool:
    """An official lyric video: the artist's own channel (or VEVO), or "official" in the title."""
    from . import youtube
    title, channel = result.get("title") or "", result.get("channel") or ""
    return bool(re.search(r"\bofficial\b", title, re.I) or re.search(r"vevo$", channel, re.I)
                or (artist and youtube.same_name(artist, channel)))

def last_resort(results: list[dict], artist: str = "", refused: set[str] = frozenset()) -> list[dict]:
    """Lyric videos to offer when nothing else can be sung: official ones first, fan uploads last."""
    lyric = [r for r in results if r.get("kind") == "lyric" and r.get("duration") and r["id"] not in refused]
    official = [r for r in lyric if is_official(r, artist or ((r.get("parsed") or {}).get("artist") or ""))]
    fan = [r for r in lyric if r not in official]
    return [{**r, "onscreen": True, "onscreen_official": r in official} for r in official + fan]
