import asyncio
from contextlib import asynccontextmanager, suppress
import io
import json
import os
import shutil
from pathlib import Path
import re
import socket
import logging
import time
import math
from urllib.parse import unquote  # karaoke-server: singer names in photo headers are URL-encoded
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from .queue import State, VERSION
from . import metrics  # karaoke-server: local usage/health metrics (cache/metrics.jsonl)
from . import lyrics, songbook, stems, worker, youtube

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(os.environ.get("KTVIBES_CACHE", ROOT / "cache"))
PORT = int(os.environ.get("KTVIBES_PORT", 8765))
state = State()

@asynccontextmanager
async def lifespan(app):
    CACHE.mkdir(parents=True, exist_ok=True)
    state.build = build()
    await asyncio.to_thread(songbook.backfill, CACHE, stems.prepared)  # caches from before the songbook
    state.restore(CACHE / "queue.json", seed=lambda: {id: meta["prepared"] for id, meta in prepared_songs().items()})
    task = asyncio.create_task(worker.run(state, CACHE))
    ahead = asyncio.create_task(worker.prefetch(state, CACHE))  # karaoke-server: download the next songs early
    metrics.setup(CACHE, VERSION)
    metrics.record("server_start", songs=len(prepared_songs()))
    room = asyncio.create_task(room_snapshots())
    try:
        yield
    finally:
        metrics.record("server_stop")
        for running in (task, ahead, room):
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running

async def room_snapshots():
    """karaoke-server metric: every 5 minutes, how busy the room is."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        metrics.record("room", phones=sum(r == "remote" for r in state.clients.values()), tv=state.player is not None,
                       sound=state.audio, queue=len(state.upcoming), playing=(state.current or {}).get("id"),
                       singers_active=sum(now - s.get("seen", 0) < 3600 for s in state.singers.values()))

def build() -> str:
    """Fingerprint of the files the pages load. A TV left open across an update reloads itself
    instead of running old code against the new server."""
    import hashlib
    digest = hashlib.sha256()
    for path in sorted((ROOT / "static").rglob("*")):
        if path.is_file():
            digest.update(path.name.encode() + path.read_bytes())
    return digest.hexdigest()[:12]

app = FastAPI(title="KTVibes", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

@app.middleware("http")
async def revalidate_static(request, call_next):
    # Pages load plain JS/CSS without a build step; revalidate so edits show up on reload.
    # Media too: a song's tracks keep their URL when converted to a new format or prepared again.
    response = await call_next(request)
    if request.url.path.startswith(("/static/", "/media/")) or request.url.path in ("/", "/tv", "/remote"):
        response.headers["Cache-Control"] = "no-cache"
    return response

# The TV is the front page: someone on a laptop can open the remote from it, but not the other way round.
@app.get("/")
@app.get("/tv")
async def tv():
    return FileResponse(ROOT / "static/tv.html")

@app.get("/remote")
async def remote():
    return FileResponse(ROOT / "static/remote.html")

@app.get("/api/state")
async def get_state():
    return state.snapshot()

@app.get("/api/suggest")
async def suggest(q: str = Query(min_length=1, max_length=200)):
    try:
        return await asyncio.to_thread(youtube.suggest, q)
    except Exception:
        return []  # completions are a nicety; never surface their failures

async def lyric_records(q: str = "", artist: str = "", title: str = "") -> list[dict]:
    """karaoke-server: every LRCLIB record for this search (or this artist + song); [] if LRCLIB is slow/down."""
    params = {"artist_name": artist, "track_name": title} if title else {"q": q}
    try:
        async with lyrics.lrclib() as client:
            response = await asyncio.wait_for(client.get("/api/search", params=params), 6)
        return response.json()
    except Exception:
        logging.getLogger("uvicorn.error").warning("LRCLIB lookup for search %r failed", q, exc_info=True)
        return []

@app.get("/api/search")
async def search(q: str = Query(min_length=1, max_length=200), page: int = Query(0, ge=0, le=9)):
    try:
        found, records = await asyncio.gather(asyncio.to_thread(youtube.search, q, page), lyric_records(q))
        check_lyrics(found, records)
        # A free-text lookup lists ~20 records, so a song can be missed ("Why" has many). Ask about the most likely
        # unconfirmed versions by artist + song (at most 3, together), then check again.
        # karaoke-server: the names most results agree on first ("good girls 5sos": four videos say "5 Seconds of
        # Summer - Good Girls", which LRCLIB knows; "5SOS - Good Girls" it doesn't)
        unsure, votes = {}, {}
        for r in found:
            p = r.get("parsed") or {}
            if not r.get("lyrics_match") and r.get("kind") not in ("cover", "live") and p.get("artist") and p.get("title"):
                key = (youtube.lead_artist(p["artist"]), youtube.song_name(p["title"]))
                unsure.setdefault(key, p)
                votes[key] = votes.get(key, 0) + 1
        unsure = dict(sorted(unsure.items(), key=lambda kv: -votes[kv[0]]))  # stable: YouTube's order among equals
        # karaoke-server: no official video of the song on the page (5,000-song study, 2026-11-22: 12% of songs, mostly
        # OPM): YouTube is asked once more for "<search> official music video", meanwhile. That found a playable
        # official video for ~40% of them ("Drake One Dance", "One Direction Night Changes").
        more_videos = asyncio.to_thread(youtube.search, f"{q} official music video") if page == 0 and needs_official(found) else None
        if unsure or more_videos:
            extra = await asyncio.gather(*(lyric_records(artist=p["artist"], title=p["title"]) for p in list(unsure.values())[:3]),
                                         *([more_videos] if more_videos else []), return_exceptions=True)
            if more_videos:
                page2 = extra.pop()
                if isinstance(page2, list):
                    seen = {r["id"] for r in found}
                    found += [r for r in page2 if r["id"] not in seen]
            extra = [more for more in extra if isinstance(more, list)]
            records = list({c.get("id") or id(c): c for c in records + [c for more in extra for c in more]}.values())
            check_lyrics(found, records)
        results = youtube.order_results(found, q)
    except Exception as exc:
        metrics.record("search_failed", q=q, page=page, error=str(exc)[-200:], bot_check=bool(youtube.BOT_CHECK.search(str(exc))))
        raise HTTPException(502, f"YouTube search failed: {youtube.explain(exc)}") from exc
    metrics.record("search", q=q, page=page, results=len(results), kinds=[r.get("kind") for r in results],
                   lyrics_match=[bool(r.get("lyrics_match")) for r in results], lyric_records=len(records))
    # Prepared songs start right away; the remote marks them.
    cached = prepared_songs()
    results = [{**result, "cached": result["id"] in cached} for result in results]
    # karaoke-server: only what can be sung: synced lyrics that match this video, or a prepared song that has them
    # (user, 2026-11-22: "Show a list that is indeed playable").
    playable = [r for r in results if r.get("lyrics_match") or (r["cached"] and lines_up(r["id"]))]
    # karaoke-server: matching songs from our own library first (first page only)
    mine = [{**s, "cached": True} for s in library_matches(q)] if page == 0 else []
    playable = mine + [r for r in playable if r["id"] not in {s["id"] for s in mine}]
    metrics.record("search_playable", q=q, page=page, shown=len(playable), hidden=len(results) - len(playable), library=len(mine))
    return playable

def needs_official(found: list[dict]) -> bool:
    """karaoke-server: the page has no official video of the song(s) it found lyrics for (or, with none, of any)."""
    songs = lambda rs: {youtube.song_name((r.get("parsed") or {}).get("title", "")) for r in rs} - {""}
    wanted = songs([r for r in found if r.get("lyrics_match")]) or songs(found)
    return bool(found) and not (wanted & songs([r for r in found if r.get("kind") == "official"]))

def check_lyrics(found: list[dict], records: list[dict]) -> None:
    """karaoke-server: clean names and "lyrics match" flags for these results from these LRCLIB records."""
    synced = [c for c in records if c.get("syncedLyrics")]
    lyrics.real_lengths(synced)
    for r in found:
        if "guessed" in (r.get("parsed") or {}):
            r["parsed"] = r["parsed"]["guessed"]  # judge again from the title, with the larger set of records
    youtube.names_from_records(found, records)
    tag = lambda c: (m.group(0) if (m := youtube.VERSION_TAG.search(c.get("trackName") or "")) else "")
    youtube.mark_lyrics(found, [(youtube.record_song(c.get("trackName", ""), c.get("artistName", "")), c["length"],
                                 youtube.artist_keys(c.get("artistName", "")), tag(c)) for c in synced if c.get("length")])

class Song(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    artist: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    singer: str = Field(default="", max_length=40)  # karaoke-server: who sings it, typed once on the remote
    rank: int | None = Field(default=None, ge=0, le=200)  # karaoke-server metric: position in the search results it came from
    kind: str = Field(default="", max_length=20)  # karaoke-server metric: that result's kind (official, lyric, ...)
    device: str = Field(default="", max_length=64)  # karaoke-server: the remote's random id; names are unique per phone

    @field_validator("singer")
    @classmethod
    def tidy_singer(cls, value):
        return " ".join(value.split())

    @field_validator("artist", "title")
    @classmethod
    def nonempty(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Artist and title must not be blank")
        return value

def prepared_songs() -> dict:
    """Cached songs with stems and confirmed details, by video id."""
    songs = {}
    for meta_path in CACHE.glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            prepared = meta_path.stat().st_mtime
        except (OSError, ValueError):
            continue
        video_id = meta_path.parent.name
        if youtube.ID.fullmatch(video_id) and meta.get("artist") and meta.get("title") and stems.prepared(meta_path.parent):
            songs[video_id] = {"id": video_id, "artist": meta["artist"], "title": meta["title"], "duration": meta.get("duration"),
                               "thumbnail": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg", "prepared": prepared, "number": meta.get("number"),
                               "source_title": meta.get("source_title") or ""}
    return songs

def lines_up(video_id: str) -> bool:
    """karaoke-server: a prepared song can be sung if it has timed lyrics whose first line was measured within 3 s
    of the singer's first phrase. Not measured (older songs): given the benefit of the doubt."""
    folder = CACHE / video_id
    try:
        timed = json.loads((folder / "timed.json").read_text(encoding="utf-8"))
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not any(line.get("text", "").strip() for line in timed.get("lines", [])):
        return False
    # karaoke-server: the measured start decides when known: a matching length is not enough, LRCLIB labels some
    # late timings with the video's length (Ashe "Stop The Wedding!", 5.8 s late; beta report 2026-11-22)
    entry = timed.get("entry_error")
    if entry is not None:
        return abs(entry) <= worker.ENTRY_LIMIT  # the detector is good to about ±1 s
    return True  # not measured (older songs): given the benefit of the doubt, as before

def library_matches(q: str, limit: int = 3) -> list[dict]:
    """karaoke-server: prepared songs with timed lyrics that match every word of the search (a word may be the start
    of one: "Pahintulo"), as search results. They start at once and their lyrics are proven, so they come first;
    songs are often sung again (the party searched "LANY Soft", "Dominga" and "i choose you" 2-3 times each)."""
    words = re.findall(r"\w+", q.casefold())
    if not words:
        return []
    found = []
    for song in prepared_songs().values():
        if not lines_up(song["id"]):
            continue
        own = re.findall(r"\w+", f"{song['artist']} {song['title']}".casefold())
        text = own + re.findall(r"\w+", song["source_title"].casefold())
        if all(any(t.startswith(w) for t in text) for w in words):
            # best first: the title is exactly the search ("You": Basil Valdez's "You" before "Love Yourself"), then
            # whole words, then the song's own artist/title over its YouTube title; newest first among equals
            exact_title = "".join(words) in ("".join(re.findall(r"\w+", song["title"].casefold())),
                                            "".join(re.findall(r"\w+", f"{song['artist']} {song['title']}".casefold())),
                                            "".join(re.findall(r"\w+", f"{song['title']} {song['artist']}".casefold())))
            found.append((not exact_title, not all(w in text for w in words), not all(any(t.startswith(w) for t in own) for w in words),
                          -song["prepared"], song))
    title = lambda s: s["source_title"] or f"{s['artist']} - {s['title']}"
    seen, unique = set(), []
    for f in sorted(found, key=lambda f: f[:4]):
        key = (youtube.lead_artist(f[-1]["artist"]), youtube.song_name(f[-1]["title"]))  # one per song
        if key not in seen:
            seen.add(key); unique.append(f)
    found = unique
    return [{"id": s["id"], "title": title(s), "channel": s["artist"], "duration": s["duration"], "thumbnail": s["thumbnail"],
             "kind": youtube.kind(title(s), s["artist"], s["artist"]), "parsed": {"artist": s["artist"], "title": s["title"]},
             "lyrics_match": True, "library": True} for *_, s in found[:limit]]

@app.get("/api/recent")
async def recent(limit: int = Query(20, ge=1, le=50)):
    """Songs that went on stage, most recent first, so the remote can re-queue them without a search."""
    songs = prepared_songs()
    played = sorted((when, video_id) for video_id, when in state.played.items() if video_id in songs)
    return [{k: v for k, v in songs[video_id].items() if k != "prepared"} for _, video_id in reversed(played[-limit:])]

@app.get("/api/songbook")
async def songbook_list():
    """Prepared songs by songbook number, for the remote's keypad and songbook (with when each was prepared, for 신곡)."""
    return sorted((s for s in prepared_songs().values() if s.get("number")), key=lambda s: s["number"])

@app.get("/api/ambient")
async def ambient():
    """Cached music videos the TV loops, muted, behind an empty stage: only songs that were prepared,
    so a video left behind by a failed download never shows up between songs."""
    return sorted(path.parent.name for path in CACHE.glob("*/video.mp4") if youtube.ID.fullmatch(path.parent.name) and stems.prepared(path.parent))

def song_meta(video_id: str) -> tuple[Path, dict]:
    folder = CACHE / video_id
    if not youtube.ID.fullmatch(video_id) or not (folder / "meta.json").is_file():
        raise HTTPException(404, "Song not found")
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    if not meta.get("artist") or not meta.get("title") or not meta.get("duration"):
        raise HTTPException(409, "This song isn't fully prepared yet")
    return folder, meta

@app.get("/api/songs/{video_id}/lyrics")
async def lyric_options(video_id: str, q: str = Query("", max_length=200)):
    """Every LRCLIB version that fits the song, best first, so the remote can pick a language or edition.
    With `q`, a free-text search instead, for songs the automatic lookup missed or got wrong."""
    folder, meta = song_meta(video_id)
    current = (folder / "lyrics.lrc").read_text(encoding="utf-8") if (folder / "lyrics.lrc").is_file() else ""
    try:
        found = await (lyrics.search(q.strip(), meta["duration"]) if q.strip() else
                       lyrics.candidates(meta["artist"], meta["title"], meta["duration"], (meta.get("source_title") or "",), exhaustive=True))
    except Exception as exc:
        raise HTTPException(502, f"Lyrics lookup failed: {exc}") from exc
    metrics.record("lyrics_picker", song=video_id, searched=bool(q.strip()), versions=len(found))
    options = []
    for c in found:
        raw = c.get("syncedLyrics") or ""
        lines = [line["text"] for line in lyrics.parse_lrc(raw) if line.get("text")]
        options.append({"id": c.get("id"), "track": c.get("trackName"), "artist": c.get("artistName"), "album": c.get("albumName"),
                        "language": lyrics.LANGUAGES[lyrics.script(raw)], "lines": len(lines), "preview": lines[:2], "current": raw == current,
                        # seconds longer (+) or shorter than this song: a big gap usually means another version
                        # (karaoke-server: by the length its timing really fits, see lyrics.real_lengths)
                        "difference": round(float(c.get("length") or c.get("duration") or 0) - meta["duration"]),
                        "fits": abs(float(c.get("length") or 0) - meta["duration"]) <= youtube.LYRICS_TOLERANCE})
    # karaoke-server: versions that line up with this video first (the in-use one keeps its place among them)
    options.sort(key=lambda o: not o["fits"])
    return options

class LyricChoice(BaseModel):
    id: int

_lyric_tasks = set()  # karaoke-server: keeps background lyric timing tasks alive

async def apply_on_stage(video_id: str, folder: Path, lrc: str, job: dict, started: float):
    """karaoke-server: time the new lyrics against the vocals in the background, then swap them in on the TV.
    The phone doesn't wait (timing can take a minute, longer while another song is being prepared). If another
    version is chosen meanwhile, the newest wins."""
    outcome = "applied"
    try:
        lines = lyrics.parse_lrc(lrc)
        lines = await asyncio.to_thread(worker.timed_lyrics, folder, lrc, stems.path(folder, "vocals"), lines, job.get("duration"))
        lyrics.line_starts(lines)
        if state.lyrics_applying.get(video_id) is not job:
            outcome = "superseded"
        elif state.current and state.current["id"] == video_id:
            state.current.update(lyrics=lines, lyrics_rev=state.current.get("lyrics_rev", 0) + 1)
            state.offset = 0
            state.lyrics_sent.clear()
        else:
            outcome = "song_ended"  # saved anyway: shown next time it plays
    except Exception:
        logging.getLogger("uvicorn.error").exception("Timing new lyrics failed for %s", video_id)
        outcome = "failed"
    finally:
        if state.lyrics_applying.get(video_id) is job:
            del state.lyrics_applying[video_id]
        metrics.record("lyrics_chosen", song=video_id, version=job["version"], playing=True, outcome=outcome,
                       seconds=round(time.monotonic() - started, 1))
        await state.broadcast()

@app.post("/api/songs/{video_id}/lyrics")
async def choose_lyrics(video_id: str, choice: LyricChoice):
    """Save the chosen version. karaoke-server: the song on stage switches to it once its words are timed, in the
    background (the reply says "applying": true); other songs use it when they play."""
    folder, meta = song_meta(video_id)
    started = time.monotonic()
    try:
        chosen = await lyrics.record(choice.id)
    except Exception as exc:
        raise HTTPException(502, f"Lyrics lookup failed: {exc}") from exc
    if not chosen or not lyrics.STAMP.search(chosen.get("syncedLyrics") or ""):
        raise HTTPException(404, "That lyrics version is no longer available")
    (folder / "lyrics.lrc").write_text(chosen["syncedLyrics"], encoding="utf-8")
    meta["lyrics_identity"] = [meta["artist"], meta["title"]]
    meta.pop("lyric_offset", None)  # a nudge for other lyrics no longer fits
    lyrics.real_lengths([chosen])  # karaoke-server: see worker.MATCHED_LENGTH (alone, only an impossible length is caught)
    meta["lyrics_duration"] = chosen.get("length")
    meta["lyrics_chosen"] = choice.id  # karaoke-server: kept as picked (worker.better_timing leaves it alone)
    worker.save_meta(folder, meta)
    if state.current and state.current["id"] == video_id:
        job = {"version": choice.id, "started": time.time(), "duration": meta["lyrics_duration"]}
        state.lyrics_applying[video_id] = job
        task = asyncio.create_task(apply_on_stage(video_id, folder, chosen["syncedLyrics"], job, started))
        _lyric_tasks.add(task); task.add_done_callback(_lyric_tasks.discard)
        await state.broadcast()
        return {"saved": choice.id, "applying": True}
    metrics.record("lyrics_chosen", song=video_id, version=choice.id, playing=False, outcome="saved",
                   seconds=round(time.monotonic() - started, 1))
    return {"saved": choice.id, "applying": False}

@app.delete("/api/songs/{video_id}")
async def delete_song(video_id: str):
    """Forget a song and delete its download; queueing it again downloads it fresh."""
    folder = CACHE / video_id
    if not youtube.ID.fullmatch(video_id) or not (folder.is_dir() or video_id in state.played):
        raise HTTPException(404, "Song not found")
    if state.queued(video_id):
        raise HTTPException(409, "Remove it from the queue first")
    state.played.pop(video_id, None)
    if folder.is_symlink():
        folder.unlink()
    elif folder.is_dir():
        await asyncio.to_thread(shutil.rmtree, folder)
    await state.broadcast()
    return {"deleted": video_id}

@app.post("/api/queue", status_code=201)
async def enqueue(song: Song):
    if len(state.upcoming) >= 100:
        raise HTTPException(409, "Queue is full")
    try:
        singer = state.claim_singer(song.singer, song.device) if song.singer else ""
        item = await state.add(song.id, song.artist, song.title, singer)
        metrics.record("queue_add", song=song.id, title=song.title, artist=song.artist, singer=singer, rank=song.rank,
                       kind=song.kind, from_search=song.rank is not None, queue=len(state.upcoming), cached=song.id in prepared_songs())
        return item
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

class Singer(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    device: str = Field(min_length=8, max_length=64)

@app.post("/api/singer")
async def claim_singer(singer: Singer):
    """karaoke-server: take (or keep) a singer name for this phone; 409 if another phone is using it."""
    try:
        name = state.claim_singer(singer.name, singer.device)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    state.save()
    return {"name": name}

@app.get("/media/{video_id}/{filename}")
async def media(video_id: str, filename: str):
    """Stems are requested by name (vocals, no_vocals) and served in whichever format is cached."""
    if not youtube.ID.fullmatch(video_id) or filename not in (*stems.NAMES, "video.mp4", "video-30.mp4"):
        raise HTTPException(404)
    folder = CACHE / video_id
    path = folder / filename if filename.endswith(".mp4") else stems.path(folder, filename)  # karaoke-server: + video-30.mp4
    if not path or not path.is_file():
        raise HTTPException(404)
    types = {".mp4": "video/mp4", ".opus": "audio/ogg", ".flac": "audio/flac", ".wav": "audio/wav"}
    return FileResponse(path, media_type=types[path.suffix])

def remote_url():
    if os.environ.get("KTVIBES_REMOTE_URL"):
        url = os.environ["KTVIBES_REMOTE_URL"].rstrip("/")
        return url if url.endswith("/remote") else url + "/remote"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            host = sock.getsockname()[0]
    except OSError:
        host = "127.0.0.1"
    return f"http://{host}:{PORT}/remote"

@app.get("/api/config")
async def config():
    return {"remote_url": remote_url(), "version": VERSION}  # karaoke-server: version too

PHOTO_TYPES = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png", b"RIFF": "image/webp"}

def photo_date(value: str) -> str:
    """karaoke-server: the date a photo was taken (YYYY-MM-DD, sent by the phone from its EXIF) for the polaroid;
    anything that isn't a real date between 1990 and tomorrow is dropped."""
    import datetime
    try:
        day = datetime.date.fromisoformat(value.strip()[:10])
    except ValueError:
        return ""
    return day.isoformat() if datetime.date(1990, 1, 1) <= day <= datetime.date.today() + datetime.timedelta(days=1) else ""

@app.post("/api/photo", status_code=201)
async def photo_upload(request: Request):
    """karaoke-server: a phone sends a (shrunk) photo; it pops up on the TV as a polaroid with the sender's name.
    The body is the image itself; X-Singer and X-Device say who sent it."""
    data = await request.body()
    kind = next((t for magic, t in PHOTO_TYPES.items() if data.startswith(magic)), None)
    if kind is None:
        raise HTTPException(415, "Send a JPEG, PNG or WebP photo")
    try:
        item = await state.add_photo(data, kind, unquote(request.headers.get("x-singer", "")), request.headers.get("x-device", ""),
                                     photo_date(request.headers.get("x-photo-date", "")))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": item["id"]}

@app.get("/api/photo/{photo_id}")
async def photo(photo_id: str):
    item = state.photos.get(photo_id)
    if item is None:
        raise HTTPException(404)
    return Response(item["data"], media_type=item["type"], headers={"Cache-Control": "private, max-age=43200"})

@app.get("/api/qr.svg")
async def qr():
    out = io.BytesIO()
    qrcode.make(remote_url(), image_factory=qrcode.image.svg.SvgPathImage).save(out)
    return Response(out.getvalue(), media_type="image/svg+xml")

@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    role = ws.query_params.get("role", "remote")
    if role == "tv" and state.player is not None:
        # Newest TV wins: a reload must not be locked out by its own stale socket.
        previous = state.player
        state.disconnect(previous)
        with suppress(Exception):
            await previous.send_json({"type": "error", "message": "Another device took over playback."})
            await previous.close(code=4001)
    state.clients[ws] = role
    if role == "tv":
        state.player = ws
        metrics.record("tv_connect", client=ws.client.host if ws.client else "")
    await state.broadcast()
    try:
        while True:
            message = await ws.receive_json()
            try:
                if not isinstance(message, dict):
                    raise ValueError("Expected a control object")
                for field in ("value", "delta", "position", "minutes"):
                    # Text values (guide modes) pass through; State.control converts numbers itself.
                    if isinstance(message.get(field), (int, float)) and not math.isfinite(message[field]):
                        raise ValueError("Expected a finite number")
                await state.control(message, ws)
            except (ValueError, TypeError, KeyError) as exc:
                await ws.send_json({"type": "error", "message": str(exc)})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if role == "tv":
            metrics.record("tv_disconnect", client=ws.client.host if ws.client else "", was_player=state.player is ws)
        state.disconnect(ws)
        await state.broadcast()


def update():
    """`ktvibes update`: pull the latest version. The next `ktvibes` (uv run) installs any new dependencies."""
    import subprocess
    import sys
    git = lambda *args: subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if not (ROOT / ".git").exists():
        sys.exit(f"{ROOT} is not a git checkout; download the new version from https://github.com/andyleenz/KTVibes")
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        sys.exit(f"{ROOT} has local changes; commit or stash them, then run ktvibes update again.")
    before = git("rev-parse", "--short", "HEAD").stdout.strip()
    pulled = git("pull", "--ff-only")
    if pulled.returncode:
        sys.exit(pulled.stderr.strip() or "git pull failed")
    after = git("rev-parse", "--short", "HEAD").stdout.strip()
    print("Already up to date." if before == after else f"Updated {before} -> {after}. Stop KTVibes if it is running, then start it again: ktvibes")

def run():
    """`uv run ktvibes`: one process only, since queue state and the model live in memory."""
    import sys
    if sys.argv[1:] == ["update"]:
        return update()
    import uvicorn
    if missing := worker.missing_tools():
        # Common right after installing: the terminal predates the install and has a stale PATH.
        print(f"WARNING: {', '.join(missing)} not found on PATH. Songs can't be prepared until it is installed.\n"
              "If you just installed it, open a new terminal and run ktvibes again.", flush=True)
    # karaoke-server: clean, quick stops. Open TV/phone sockets never close by themselves, so uvicorn waited until
    # systemd killed it ("Failed with result 'timeout'" on every restart). Give them 3 s, save the queue, then exit
    # without waiting for a song mid-separation in a worker thread (it is prepared again on the next start).
    uvicorn.run(app, host="0.0.0.0", port=PORT, timeout_graceful_shutdown=3)
    state.save()
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0)
