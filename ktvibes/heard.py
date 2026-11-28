"""karaoke-server: are these the words that are sung? An independent check of a song's lyrics (user, 2026-11-28).

The aligner places any words somewhere, so it can't tell wrong lyrics from right ones (study, research/lyric-sync in
the notes repo: "Papahiram" aligned 85% of its lines with lyrics the singer never sings). A speech recogniser can:
Whisper writes down what it hears in the lead vocals, with word times, and a lyric line "agrees" when at least half
of its words are heard within PAD seconds of where the line is shown. On the 40 songs the room had voted on, songs
with wrong lyrics or another version's lyrics agreed on 0-29% of their lines, every song voted in sync on 71-100%.

Whisper's guess of the language decides what it hears ("Ikaw at Ako" was taken for French: 17%; as Tagalog: 100%), so a
low score is checked again with each of RETRY languages before it counts. Lyrics not written in Latin letters are not
judged. The transcript is kept beside the stems (heard.json), so other lyrics for the same song are judged at once.
The model is loaded for the check and freed again: the GPU's memory is needed by the separation models."""
import difflib
import json
import logging
import os
from pathlib import Path
import re
import threading
import unicodedata

log = logging.getLogger(__name__)
MODEL = "large-v3-turbo"
ENABLED = os.environ.get("KTVIBES_LYRICS_CHECK", "on").lower() != "off"
PAD = 2.0  # seconds around a line in which its words must be heard
MIN_AGREE = 0.5  # lyrics with fewer agreeing lines are not this recording's lyrics
MIN_HEARD = 8  # lines with singing heard near them needed for a verdict
RETRY = ("en", "tl")  # the languages sung here; a low score is checked again as each of them
_lock = threading.Lock()

def norm(word: str) -> str:
    word = unicodedata.normalize("NFKD", str(word).lower())
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in word if not unicodedata.combining(c)))

def similar(a: str, b: str) -> bool:
    return a == b or (len(a) >= 4 and len(b) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.75)

def agreement(lines: list[dict], words: list[dict]) -> dict:
    """{"heard": lines with singing heard near them, "agree": share of those whose words are what was heard}."""
    said = [(norm(w["w"]), w["s"]) for w in words if norm(w["w"])]
    agree = heard = 0
    for line in lines:
        units = [u for u in (line.get("units") or []) if norm(u[0])]
        if len(units) < 3:
            continue
        start, end = units[0][1] - PAD, units[-1][2] + PAD
        near = [w for w, t in said if start <= t <= end]
        if len(near) < 2:
            continue
        heard += 1
        agree += sum(any(similar(norm(u[0]), w) for w in near) for u in units) >= len(units) / 2
    return {"heard": heard, "agree": round(agree / heard, 2) if heard else None}

# The commonest words of the languages sung here. Whisper guesses the language from the first 30 s, which in a
# vocal stem is often an intro with little singing ("Ikaw at Ako" was taken for French), so the lyrics say it instead.
COMMON = {"en": set("the you and i to a me my in it of that is on your we be for all im dont love know with so but its when this just".split()),
          "tl": set("ang ng sa ko mo ka ako ikaw at na hindi lang kung mga ito siya naman ay ni kay para pa din rin ba di sayo akin ating tayo".split())}

def written(lines: list[dict]) -> str | None:
    """The language the lyrics are written in, when it is clearly one of RETRY; else None (Whisper guesses)."""
    words = [norm(w) for line in lines for w in str(line.get("text") or "").split()]
    words = [w for w in words if w]
    if len(words) < 20:
        return None
    share = {language: sum(w in common for w in words) / len(words) for language, common in COMMON.items()}
    first, second = sorted(share, key=share.get, reverse=True)[:2]
    return first if share[first] >= 0.15 and share[first] >= 1.5 * share[second] else None

def failed(check: dict | None) -> bool:
    """True when the check says these are not the song's lyrics (no check, or too little heard: no verdict)."""
    return bool(check) and check.get("agree") is not None and check.get("heard", 0) >= MIN_HEARD and check["agree"] < MIN_AGREE

def _transcribe(vocals: Path, languages: list) -> list[dict]:
    """One transcript per language asked for (None: Whisper's own guess): [{"language", "asked", "words"}]."""
    import torch
    import whisper
    with _lock:
        model = whisper.load_model(MODEL, device="cuda" if torch.cuda.is_available() else "cpu")
        try:
            out = []
            for language in languages:
                result = model.transcribe(str(vocals), word_timestamps=True, condition_on_previous_text=False,
                                          fp16=torch.cuda.is_available(), language=language)
                out.append({"language": result.get("language"), "asked": language,
                            "words": [{"w": w["word"], "s": round(w["start"], 2), "e": round(w["end"], 2)}
                                      for segment in result["segments"] for w in segment.get("words", [])]})
            return out
        finally:
            del model
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

def check(vocals: Path, lines: list[dict]) -> dict | None:
    """{"agree", "heard", "language"} for these timed lines against what is sung in `vocals`, or None when there is
    nothing to judge (the check is off or unavailable, no lines in Latin letters). Transcripts are cached."""
    if not ENABLED or sum(len([u for u in (line.get("units") or []) if norm(u[0])]) >= 3 for line in lines) < MIN_HEARD:
        return None
    vocals = Path(vocals)
    cache = vocals.parent / "heard.json"
    key = f"{MODEL}:{vocals.name}:{vocals.stat().st_mtime_ns}:{vocals.stat().st_size}"
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        transcripts = saved["transcripts"] if saved.get("key") == key else []
    except (OSError, ValueError, KeyError):
        transcripts = []

    def best():
        scored = [(agreement(lines, t["words"]), t) for t in transcripts]
        scored = [(a, t) for a, t in scored if a["agree"] is not None]
        if not scored:
            return None
        a, t = max(scored, key=lambda pair: (pair[0]["agree"], pair[0]["heard"]))
        return {**a, "language": t["language"]}

    def listen(languages):
        languages = [l for l in languages if not any(t["asked"] == l or (l and t["language"] == l) for t in transcripts)]
        if languages:
            transcripts.extend(_transcribe(vocals, languages))
            temporary = cache.with_suffix(".tmp")
            temporary.write_text(json.dumps({"key": key, "transcripts": transcripts}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cache)

    try:
        result = None
        # the lyrics' own language first; a low score is checked with Whisper's guess and the other languages sung
        # here before it counts (a wrong language hears other words)
        for language in dict.fromkeys((written(lines), None, *RETRY)):
            listen([language])
            result = best()
            if result is not None and not failed(result) and result["heard"] >= MIN_HEARD:
                break
        return result
    except Exception:
        log.exception("Lyrics check unavailable for %s", vocals.parent.name)
        return None

if __name__ == "__main__":  # python -m ktvibes.heard --download: fetch the model (install-native.sh)
    import sys
    if "--download" in sys.argv:
        import whisper
        whisper._download(whisper._MODELS[MODEL], os.path.expanduser("~/.cache/whisper"), False)
        print("ready:", MODEL)
