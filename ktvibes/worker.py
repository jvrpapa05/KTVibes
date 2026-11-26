import asyncio
import hashlib
import json
import logging
import sys
import time
from pathlib import Path
import soundfile as sf
from . import align, guides, songbook, spotify, stems, youtube, lyrics, metrics

log = logging.getLogger(__name__)
TIMING_VERSION = 10  # bump when alignment or guide output changes, so cached timing is rebuilt
LYRICS_RECHECK = 7 * 86400  # a song LRCLIB had no lyrics for is looked up again after this long
STARTED = time.time()  # energy-only timing (aligner failed) cached before this run is retried once

# karaoke-server: the remote shows one 0-100% ring per song. Separating and syncing report no progress of their
# own, so each gets a start time and an expected length (seconds per second of song, learned from real runs;
# the defaults are what this CPU measured), and the remote fills the ring as time passes.
STEP_RATE = {"separating": 0.36, "syncing": 0.17}

def expect_step(item, step, duration):
    item["since"] = time.time()
    item["expect"] = round(max(3.0, STEP_RATE[step] * float(duration or 210)), 1)

def learn_step(step, seconds, duration):
    if duration and seconds and seconds > 0:
        STEP_RATE[step] = round(0.6 * STEP_RATE[step] + 0.4 * seconds / float(duration), 4)

def separate(source, destination):
    # Keep model imports off the server startup path.
    from .separate import separate as separate_audio
    return separate_audio(source, destination)

def reporter(state, item, step):
    """Progress callback for worker threads: while `step` is the one on show, record the fraction; broadcast at most twice a second."""
    loop, last = asyncio.get_running_loop(), [0.0]
    def report(fraction):
        def apply():
            if item.get("step") != step:
                return
            item["progress"] = fraction
            if loop.time() - last[0] >= 0.5:
                last[0] = loop.time()
                asyncio.ensure_future(state.broadcast())
        loop.call_soon_threadsafe(apply)
    return report

def missing_tools() -> list[str]:
    """External programs KTVibes needs on PATH: ffmpeg/ffprobe for media, Node.js for YouTube extraction."""
    import shutil
    return [name for name, exe in (("ffmpeg", "ffmpeg"), ("ffprobe", "ffprobe"), ("Node.js", "node")) if not shutil.which(exe)]

def save_meta(folder: Path, meta: dict):
    temporary = folder / "meta.tmp"
    temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(folder / "meta.json")

def fetch_video(video_id, folder, progress):
    """Runs alongside the audio work; a missing video only means the stage background."""
    try:
        youtube.download_video(video_id, folder, progress)
    except Exception:
        log.exception("Video unavailable for %s", video_id)
        return "Video unavailable; using the stage background."

# karaoke-server: lyrics whose recording is as long as this video (within MATCHED_LENGTH s) are already in place, so
# timing may only nudge them by that much. Unlimited, it moved a perfectly matched Elvis song 47 s late (onto a
# repeat of the melody); a big shift is only for videos that really are longer (a story intro).
MATCHED_LENGTH = 5.0

def timed_lyrics(folder: Path, raw: str, samples_path: Path, lines: list[dict], lyric_duration: float | None = None) -> list[dict]:
    """Word timing and guides for these lyrics, reused from timed.json when the LRC text is unchanged."""
    key = f"{TIMING_VERSION}:{hashlib.sha256(raw.encode()).hexdigest()}"
    try:
        cached = json.loads((folder / "timed.json").read_text(encoding="utf-8"))
        # Energy-only timing (the aligner failed) is retried once per run, not on every play.
        if cached.get("key") == key and (cached.get("aligned", True) or (folder / "timed.json").stat().st_mtime >= STARTED):
            return cached["lines"]
    except (OSError, ValueError, KeyError):
        pass
    samples, rate = sf.read(samples_path, dtype="float32")
    energy = lyrics.envelope(samples, rate)
    matched = bool(lyric_duration) and abs(len(samples) / rate - lyric_duration) <= MATCHED_LENGTH
    if shift := (lyrics.find_shift(lines, energy, limit=MATCHED_LENGTH, far=False) if matched else lyrics.find_shift(lines, energy)):
        log.info("Lyrics for %s shifted %+.2fs to match the vocals", folder.name, shift)
        lyrics.shift_lines(lines, shift)
    if any(moved := lyrics.shift_sections(lines, energy)):
        log.info("Lyric sections for %s moved %s to match the vocals", folder.name, moved)
    aligned = True
    try:
        # Forced alignment on the vocals; lines it cannot place keep the energy estimate.
        align.align(lines, samples, rate)
    except Exception:
        log.exception("Forced alignment unavailable for %s", folder.name)
        aligned = False
    lyrics.time_units(lines, energy)
    lines = lyrics.add_jyutping(guides.add_hangul(lyrics.add_korean_romanization(lyrics.add_pinyin(lyrics.add_romaji(lines)))))
    temporary = folder / "timed.tmp"
    # karaoke-server: how far the first line is from the singer's first phrase; search only offers library songs
    # whose lyrics line up (see main.lines_up)
    entry = lyrics.entry_error(samples, rate, lines)
    gaps = lyrics.uncovered(samples, rate, lines, len(samples) / rate)  # karaoke-server: sung parts with no lyrics
    temporary.write_text(json.dumps({"key": key, "aligned": aligned, "entry_error": entry, "uncovered": gaps, "lines": lines}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(folder / "timed.json")
    return lines

# karaoke-server (beta report 2026-11-22, Ashe "Stop The Wedding!"): LRCLIB had the album timing (first line 0:07.5)
# and one labelled with the video's length but timed 7 s later; the length matched, so the late one was used. When
# the first line lands more than ENTRY_LIMIT from the singer's first phrase, the song's other timings are tried and
# the one that starts with the singer is kept.
ENTRY_LIMIT = 3.0

def vote_verdict(folder: Path, meta: dict) -> str | None:
    """karaoke-server: what the room said about these lyrics (queue.State.cast_vote): "confirmed" (2+ in sync, more than
    not), "flagged" (2+ not in sync, more than in sync) or None. Only votes about the current lyrics.lrc count."""
    try:
        lrc = (folder / "lyrics.lrc").read_text(encoding="utf-8")
    except OSError:
        return None
    mine = [v for v in meta.get("lyrics_votes", []) if v.get("lyrics") == hashlib.sha256(lrc.encode()).hexdigest()[:16]]
    up, down = sum(v.get("up", 0) for v in mine), sum(v.get("down", 0) for v in mine)
    return "flagged" if down >= 2 and down > up else "confirmed" if up >= 2 and up > down else None

async def better_timing(folder: Path, raw: str, identity: list, duration: float, meta: dict):
    """(raw, lines) of another LRCLIB timing that starts with the singer, or None."""
    try:
        entry = json.loads((folder / "timed.json").read_text(encoding="utf-8")).get("entry_error")
    except (OSError, ValueError):
        return None
    tried_key = hashlib.sha256(raw.encode()).hexdigest()[:16]
    # a version someone picked on the remote stays; each timing is looked at once
    verdict = vote_verdict(folder, meta)  # the room's votes outrank the start detector
    tried_key += ":" + (verdict or "")
    off = verdict == "flagged" or (entry is not None and abs(entry) > ENTRY_LIMIT)
    if not off or verdict == "confirmed" or not raw or meta.get("lyrics_chosen") or meta.get("timings_tried") == tried_key:
        return None
    meta["timings_tried"] = tried_key
    entry = entry or 0.0
    first = lambda text: (lyrics.span({"syncedLyrics": text}) or (None,))[0]
    now = first(raw)
    try:
        found = await lyrics.candidates(*identity, duration, (meta.get("source_title") or "",), exhaustive=True)
    except Exception:
        log.exception("Other lyric timings unavailable for %s", folder.name)
        return None
    lyrics.real_lengths([c for c in found if c.get("syncedLyrics")])
    # The start detector can be fooled by humming or a chant before the first line ("Anong Daratnan": 11.5 s, yet
    # fine), so another timing is used only when more LRCLIB records agree on its start than on the current one's.
    try:  # every record of the song, not only the distinct candidates
        async with lyrics.lrclib() as client:
            every = (await client.get("/api/search", params={"artist_name": identity[0], "track_name": identity[1]})).json()
    except Exception:
        every = []
    starts = [first(c.get("syncedLyrics") or "") for c in list(every) + found if c.get("syncedLyrics")]
    backing = lambda start: sum(x is not None and abs(x - start) <= 1.0 for x in starts)
    seen, options = {raw}, []
    for c in found:
        text = c.get("syncedLyrics") or ""
        start = first(text)
        if text in seen or start is None or now is None or abs(start - now) < 1.5 or not lyrics.fits(c, duration) \
                or backing(start) <= backing(now):
            continue
        seen.add(text)
        options.append((abs(start - (now - entry)), c))  # closest to where the singer really starts, first
    best = (abs(entry), None, None, None)
    trial = folder / "timing-trial"
    for _, c in sorted(options, key=lambda o: o[0])[:2]:
        trial.mkdir(exist_ok=True)
        (trial / "timed.json").unlink(missing_ok=True)
        text = c["syncedLyrics"]
        lines = await asyncio.to_thread(timed_lyrics, trial, text, stems.path(folder, "vocals"), lyrics.parse_lrc(text), c.get("length"))
        tried = json.loads((trial / "timed.json").read_text(encoding="utf-8")).get("entry_error")
        log.info("Lyrics for %s: first line %+.1fs from the singer; LRCLIB %s gives %s", folder.name, entry, c.get("id"), tried)
        if tried is not None and (abs(tried) < best[0] - 1 or (verdict == "flagged" and abs(tried) <= ENTRY_LIMIT and best[1] is None)):
            best = (abs(tried), text, lines, c)
            (trial / "timed.json").replace(folder / "timed.json")
    if trial.exists():
        for f in trial.iterdir():
            f.unlink()
        trial.rmdir()
    if best[1] is None:
        return None
    (folder / "lyrics.lrc").write_text(best[1], encoding="utf-8")
    meta["lyrics_duration"] = best[3].get("length") or best[3].get("duration")
    meta["timings_tried"] = hashlib.sha256(best[1].encode()).hexdigest()[:16] + ":"
    meta.pop("lyric_offset", None)
    return best[1], best[2]

def sung_without_lyrics(folder: Path, lines: list[dict]) -> list:
    """karaoke-server: timed.json's "uncovered" stretches (lyrics.uncovered), worked out once for songs timed before
    it existed."""
    path = folder / "timed.json"
    try:
        timed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if "uncovered" not in timed:
        try:
            samples, rate = sf.read(stems.path(folder, "vocals"), dtype="float32")
            timed["uncovered"] = lyrics.uncovered(samples, rate, lines, len(samples) / rate)
            temporary = folder / "timed.tmp"
            temporary.write_text(json.dumps(timed, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except Exception:
            log.exception("Couldn't check %s for sung parts without lyrics", folder.name)
            return []
    return timed["uncovered"]

class Removed(Exception):
    """The song left the queue while it was being prepared."""

# Video downloads still running, by video id: a song queued again reuses its download instead of starting a second.
videos = {}
# karaoke-server: audio downloads still running, by video id (the prefetcher's), so the worker waits for one
# instead of starting a second download of the same song.
audio_jobs = {}
PREFETCH_AHEAD = 3  # queued songs whose audio and video download ahead, while an earlier song is being separated
PREFETCH_PAUSE = 2  # seconds between looks at the queue

def fetch_audio(video_id, folder):
    """Download a song's audio and keep what yt-dlp says about it (duration, title) in its meta.json."""
    result = youtube.download(video_id, folder)
    try:
        meta = json.loads((folder / "meta.json").read_text())
    except (OSError, ValueError):
        meta = {}
    meta.update(result)
    save_meta(folder, meta)
    return result

async def prefetch(state, cache: Path):
    """karaoke-server: download the next few queued songs' audio and video in the background, one file at a time.
    Downloads use the network, not the CPU, so they never slow the song being separated; when a song's turn
    comes it can start separating at once. A failure here is harmless: the worker downloads it itself later."""
    while True:
        await asyncio.sleep(PREFETCH_PAUSE)
        for item in [i for i in state.upcoming if i["status"] == "queued"][:PREFETCH_AHEAD]:
            folder = cache / item["id"]
            try:
                if stems.ready(folder):
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                if not (folder / "audio.m4a").is_file() and item["id"] not in audio_jobs:
                    job = audio_jobs[item["id"]] = asyncio.ensure_future(asyncio.to_thread(fetch_audio, item["id"], folder))
                    try:
                        await job
                    finally:
                        audio_jobs.pop(item["id"], None)
                    log.info("Prefetched audio for %s", item["id"])
                    break  # one download per pass; look at the queue again
                if not (folder / "video.mp4").is_file() and (item["id"] not in videos or videos[item["id"]].done()):
                    job = videos[item["id"]] = asyncio.ensure_future(asyncio.to_thread(fetch_video, item["id"], folder, None))
                    await job
                    log.info("Prefetched video for %s", item["id"])
                    break
            except Exception:
                log.warning("Prefetch failed for %s; it will be downloaded when its turn comes", item["id"], exc_info=True)
                metrics.record("prefetch_failed", song=item["id"], error=str(sys.exc_info()[1])[-300:])

async def run(state, cache: Path):
    while True:
        state.wake.clear()
        item = next((i for i in state.upcoming if i["status"] == "queued"), None)
        if item is None:
            await state.wake.wait()
            continue
        folder = cache / item["id"]
        timing, separated = {}, False  # karaoke-server: for the song_prepared metric
        def check():
            # A removed song stops holding up the queue; a download already running finishes on its own.
            if item is not state.current and item not in state.upcoming:
                raise Removed
        try:
            folder.mkdir(parents=True, exist_ok=True)
            meta_path = folder / "meta.json"
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, ValueError):
                meta = {}
            # The (larger, optional) video downloads while the audio is fetched and separated.
            video = None
            if not (folder / "video.mp4").is_file():
                video = videos.get(item["id"])
                if video is None or video.done():
                    video = videos[item["id"]] = asyncio.ensure_future(asyncio.to_thread(fetch_video, item["id"], folder, reporter(state, item, "video")))
            if not stems.ready(folder):
                # Songs cached as WAV or FLAC convert in seconds instead of separating again.
                gain = await asyncio.to_thread(stems.migrate, folder, meta.get("stem_gain"))
                if gain is None:
                    if not (folder / "audio.m4a").is_file() and (job := audio_jobs.get(item["id"])) and not job.done():
                        # karaoke-server: the prefetcher is already downloading it: wait for that, then read its meta.
                        item["status"], item["step"], item["progress"] = "downloading", "audio", 0.0
                        await state.broadcast()
                        while not job.done():
                            await asyncio.wait({job}, timeout=0.5)
                            check()
                        try:
                            meta.update(json.loads(meta_path.read_text()))
                        except (OSError, ValueError):
                            pass
                    if not (folder / "audio.m4a").is_file():
                        item["status"], item["step"], item["progress"] = "downloading", "audio", 0.0
                        await state.broadcast()
                        t_download = time.monotonic()
                        meta.update(await asyncio.to_thread(youtube.download, item["id"], folder, reporter(state, item, "audio")))
                        timing["download_s"] = round(time.monotonic() - t_download, 1)
                        check()
                    item["status"], item["step"], item["progress"] = "separating", None, None
                    expect_step(item, "separating", meta.get("duration"))
                    await state.broadcast()
                    meta["separation_seconds"], gain = await asyncio.to_thread(separate, folder / "audio.m4a", folder)
                    separated = True
                    learn_step("separating", meta["separation_seconds"], meta.get("duration"))
                meta["stem_gain"] = gain
                save_meta(folder, meta)  # the gain must survive even if a later step fails
            check()
            if video and not video.done():
                item["status"], item["step"], item["progress"] = "downloading", "video", 0.0
                await state.broadcast()
                while not video.done():
                    await asyncio.wait({video}, timeout=0.5)
                    check()
            if video and (warning := await video):
                item["video_warning"] = warning
            item["video"] = (folder / "video.mp4").is_file()
            item["video_30"] = (folder / "video-30.mp4").is_file()  # karaoke-server: 30 fps fallback for 60 fps videos
            item["duration"] = sf.info(stems.path(folder, "no_vocals")).duration
            # Where the song stops, before any trailing silence: Classic scores it there. Remembered per stem
            # file, and left unset (the TV waits for the file's end) if a stem can't be read.
            stamps = [p.stat().st_mtime for name in stems.NAMES if (p := stems.path(folder, name))]
            meta.pop("music_end", None)  # the old instrumental-only value
            known = meta.get("song_end")
            if stamps and not (isinstance(known, list) and len(known) == 2 and known[0] == stamps):
                end = await asyncio.to_thread(stems.song_end, folder)
                known = meta["song_end"] = [stamps, end] if end else None
            if stamps and known:
                item["music_end"] = known[1]
            item["gain"] = float(meta.get("stem_gain") or 1)
            item["status"], item["step"], item["progress"] = "syncing", None, None
            expect_step(item, "syncing", item["duration"])
            syncing_from = time.monotonic()
            await state.broadcast()
            identity = [item["artist"], item["title"]]
            cached = (folder / "lyrics.lrc").read_text(encoding="utf-8") if (folder / "lyrics.lrc").is_file() else ""
            raw = ""
            same = meta.get("lyrics_identity") == identity
            if same and lyrics.parse_lrc(cached):
                raw = cached
                item["lyrics"] = lyrics.parse_lrc(cached)
            elif same and time.time() - meta.get("lyrics_checked", 0) < LYRICS_RECHECK:
                pass  # LRCLIB had none recently; "Lyrics language…" on the remote still searches
            else:
                try:
                    result = await lyrics.fetch(*identity, item["duration"], (meta.get("source_title") or "",))
                    raw, item["lyrics"] = result["raw"], result["lines"]
                    meta["lyrics_duration"] = result.get("duration")
                    # A miss is remembered for a week, so a later LRCLIB addition is still picked up.
                    meta["lyrics_identity"], meta["lyrics_checked"] = identity, time.time()
                    meta.pop("lyric_offset", None)  # a nudge for other lyrics no longer fits
                    meta.pop("lyrics_chosen", None)
                    if result["raw"]:
                        (folder / "lyrics.lrc").write_text(result["raw"], encoding="utf-8")
                    else:
                        (folder / "lyrics.lrc").unlink(missing_ok=True)
                except Exception:
                    log.exception("Lyrics unavailable for %s", item["id"])
                    item["lyrics_warning"] = "Lyrics unavailable; audio is ready."
            if item["lyrics"]:
                try:
                    item["lyrics"] = await asyncio.to_thread(timed_lyrics, folder, raw, stems.path(folder, "vocals"), item["lyrics"], meta.get("lyrics_duration"))
                    if (better := await better_timing(folder, raw, identity, item["duration"], meta)):
                        raw, item["lyrics"] = better
                    item["uncovered"] = await asyncio.to_thread(sung_without_lyrics, folder, item["lyrics"])
                    lyrics.line_starts(item["lyrics"])  # cheap, so kept out of the timing cache
                except Exception:
                    log.exception("Word timing unavailable for %s", item["id"])
            item["offset"] = float(meta.get("lyric_offset") or 0)
            # karaoke-server: a lyric video (its own words on screen) can be swapped for scenery on the TV
            # the kind search gave when it was queued (it saw the channel); otherwise read off the YouTube title
            item["kind"] = meta["kind"] = item.get("kind") or meta.get("kind") or youtube.kind(meta.get("source_title") or "")
            # scenery may replace a lyric video (its own words clash) or an audio upload (a still picture), user 2026-11-22
            item["scenery_ok"] = item["kind"] in ("lyric", "audio")
            # karaoke-server: the album cover from Spotify, looked up once per song (kept in meta.json)
            if "cover" not in meta and spotify.enabled():
                try:
                    meta["cover"] = await asyncio.to_thread(spotify.album_cover, item["artist"], item["title"])
                except Exception:
                    log.warning("No album cover for %s", item["id"], exc_info=True)
            if meta.get("cover"):
                item["cover"] = meta["cover"]
            meta.update(duration=item["duration"], artist=item["artist"], title=item["title"])
            item["number"] = songbook.assign(cache, meta)
            save_meta(folder, meta)
            check()
            learn_step("syncing", time.monotonic() - syncing_from, item["duration"])
            item["status"], item["step"], item["progress"] = "ready", None, None
            # karaoke-server: how long this song took to get ready, and what came out of it
            try:
                timed = json.loads((folder / "timed.json").read_text()) if (folder / "timed.json").is_file() else {}
            except (OSError, ValueError):
                timed = {}
            video_file = folder / "video.mp4"
            metrics.record("song_prepared", song=item["id"], title=item["title"], artist=item["artist"], singer=item.get("singer", ""),
                           duration=round(item.get("duration") or 0), waited_s=round(time.time() - item.get("added", time.time()), 1),
                           syncing_s=round(time.monotonic() - syncing_from, 1), separated=separated,
                           separation_s=round(meta.get("separation_seconds") or 0, 1) if separated else 0, **timing,
                           lyrics_lines=len(item.get("lyrics") or []), aligned=timed.get("aligned"),
                           video=item.get("video", False), video_fps=round(youtube.video_fps(video_file)) if video_file.is_file() else 0,
                           video_30=item.get("video_30", False), video_warning=item.get("video_warning", ""))
            item.pop("since", None); item.pop("expect", None)
            state.promote()
        except Removed:
            # Undoing the removal puts it back as queued, to be prepared again (fast from cache).
            log.info("Stopped preparing %s: removed from the queue", item["id"])
            item["status"], item["step"], item["progress"] = "queued", None, None
        except Exception as exc:
            log.exception("Preparation failed for %s", item["id"])
            item["status"] = "error"
            missing = missing_tools()
            # A missing ffmpeg or Node.js surfaces as an obscure "WinError 2"; say what to fix instead.
            item["error"] = f"{' and '.join(missing)} not found. Install, open a new terminal, restart KTVibes, then Retry." if missing else youtube.explain(exc)
            metrics.record("prep_error", song=item["id"], title=item["title"], error=str(exc)[-300:], bot_check=bool(youtube.BOT_CHECK.search(str(exc))))
            state.promote()
        await state.broadcast()
