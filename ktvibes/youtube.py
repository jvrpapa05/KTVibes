"""YouTube search/download. Run: uv run python -m ktvibes.youtube QUERY"""
import json
import os
import re
import sys
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

def suggest(query: str) -> list[str]:
    """YouTube's own search-box completions for a partial query."""
    url = "https://suggestqueries.google.com/complete/search?" + urllib.parse.urlencode({"client": "firefox", "ds": "yt", "q": query})
    with urllib.request.urlopen(url, timeout=3) as response:
        return [s for s in json.loads(response.read().decode("utf-8", "replace"))[1] if isinstance(s, str) and not NOT_MUSIC.search(s)][:8]

def parse_title(title: str, channel: str = "") -> dict:
    clean = re.sub(r"\s*[\[(（【][^\])）】]*(?:official|music video|lyrics?|mv|audio|4k|hd|hq|remaster(?:ed)?|官方|歌詞|歌词|뮤직비디오)[^\])）】]*[\])）】]", "", title, flags=re.I).strip()
    clean = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", clean).strip()  # emoji: "(Lyrics) 🎵"
    clean = re.sub(r"\s*(?:[-–—|｜]|//)?\s*(?:official\s+)?(?:music\s+video|mv|lyric video|lyrics?)\s*$", "", clean, flags=re.I).strip()
    # karaoke-server: a trailing "| Official Performance Video", "| Official Video", "| Lyric Visualizer"... after a pipe
    # (never a dash, which separates artist and title) is not part of the song's name; it broke the lyrics lookup.
    clean = re.sub(r"\s*(?:[|｜]|//)\s*[^|｜]*\b(?:official|performance video|lyric video|visuali[sz]er|music video)\b[^|｜]*$", "", clean, flags=re.I).strip()
    bracketed = re.match(r"(.+?)\s*【([^】]+)】", clean)
    if bracketed:  # "周杰倫 Jay Chou【晴天 Sunny Day】", even with a trailing "華視偶像劇…片尾曲"
        return {"artist": bracketed[1], "title": bracketed[2]}
    parts = re.split(r"\s+[-–—｜|]\s+", clean, maxsplit=1)
    channel = re.sub(r"\s*- Topic$", "", channel)
    if len(parts) == 2 and channel and same_name(parts[1], channel) and not same_name(parts[0], channel):
        parts.reverse()  # "Numb [4K Upgrade] - Linkin Park" on the Linkin Park channel: title first
    return {"artist": parts[0] if len(parts) == 2 else channel, "title": parts[-1]}

def same_name(a: str, b: str) -> bool:
    """Loose name match, ignoring case, spaces, punctuation and a "VEVO"/"Official" suffix."""
    key = lambda s: re.sub(r"(?:vevo|official)$", "", re.sub(r"\W", "", s.casefold()))
    return bool(key(a)) and key(a) == key(b)

# Searches are for songs to sing: these are rarely the song itself, or lack the vocals that separation needs.
NOT_MUSIC = re.compile(r"\b(?:reactions?|react(?:s|ing)?|dance practice|dance break|choreography|tutorial|lesson|how to|"
                       r"compilation|playlist|mix|nonstop|full album|karaoke|instrumental|inst\.|8d audio|sped up|slowed|"
                       r"behind the scenes|interview|vlog|teaser|trailer|shorts?)\b|#shorts", re.I)

def is_music(title: str, duration) -> bool:
    """A plausible single song: 1-10 minutes, and not a reaction, lesson, mix or similar."""
    return not NOT_MUSIC.search(title or "") and (duration is None or 60 <= duration <= 600)

# karaoke-server: each search result gets a kind, shown as a label on the remote, and a page is re-ordered so official
# music videos come first (they give the best room experience), then lyric videos, audio, others and live recordings.
# YouTube's own order is kept within each kind. Lyric/audio/live words are checked before "official", so an
# "Official Lyric Video" counts as a lyric video and an "Official Audio" as audio.
KIND_ORDER = {"official": 0, "lyric": 1, "audio": 2, "other": 3, "cover": 4, "live": 5}
# karaoke-server: someone else singing it ("(X Cover)", "cover by", "// X cover", 'covers "Song"', "#cover"), so the
# guide vocals aren't the original singer's. Only in those shapes, so titles like "Cover Me" or "Take Cover" stay.
COVER = re.compile(r"[(\[][^)\]]*\bcover\b[^)\]]*[)\]]|\bcover(?:ed)? by\b|(?<!take )\bcover\s*(?:$|[|/\-#])|\bcovers\s*[\"“']"
                   r"|[\"“]cover[\"”]|#cover\b", re.I)
LYRIC = re.compile(r"\blyrics?\b|lyric video|\(lyric\)|sing-?along|가사|歌詞|歌词", re.I)
AUDIO = re.compile(r"official audio|\baudio\b|visuali[sz]er", re.I)
LIVE = re.compile(r"\blive\b|live at|live session|\bconcert\b|\bunplugged\b", re.I)
# Learned from our own library (2026-11-21): "Official HD Music Video", "Official Performance Video", a bare
# "(Official)" and "(Music Video)" are official too; "(Roblox Music Video)" is not (only a bare bracket counts).
OFFICIAL = re.compile(r"official\b[^()\[\]|]{0,25}?\bvideo\b|[\[(]\s*official\s*[\])]|[\[(]\s*music video\s*[\])]|\bm/?v\b|official mv|뮤직비디오", re.I)

def kind(title: str, channel: str = "", artist: str = "") -> str:
    """What sort of upload a search result is: official, lyric, audio, cover, live or other."""
    title, channel = title or "", channel or ""
    if COVER.search(title):
        return "cover"
    if LYRIC.search(title):
        return "lyric"
    if AUDIO.search(title) or channel.endswith(" - Topic"):
        return "audio"
    if LIVE.search(title):
        return "live"
    if OFFICIAL.search(title) or re.search(r"vevo$", channel, re.I) or (artist and same_name(artist, channel)):
        return "official"
    return "other"

def search(query: str, page: int = 0, size: int = 10) -> list[dict]:
    """One page of results; YouTube search has no offset, so later pages fetch the earlier ones too."""
    import yt_dlp
    first, last = page * size + 1, (page + 1) * size
    with yt_dlp.YoutubeDL({**base_options(), "extract_flat": True, "quiet": True, "noprogress": True, "skip_download": True,
                           "socket_timeout": 20, "playlist_items": f"{first}-{last}"}) as ydl:
        result = ydl.extract_info(f"ytsearch{last}:{query}", download=False)
    results = [{"id": e["id"], "title": e.get("title", "Untitled"), "channel": e.get("channel") or e.get("uploader", ""),
             "duration": e.get("duration"), "thumbnail": f'https://i.ytimg.com/vi/{e["id"]}/mqdefault.jpg',
             "parsed": parse_title(e.get("title", ""), e.get("channel") or e.get("uploader", ""))}
            for e in result.get("entries", []) if e and ID.fullmatch(e.get("id", "")) and is_music(e.get("title", ""), e.get("duration"))]
    for r in results:
        r["kind"] = kind(r["title"], r["channel"], r["parsed"].get("artist", ""))
    return order_results(results)  # karaoke-server: main.search orders again after the lyrics check

# karaoke-server: synced lyrics fit a video when it is as long as the recording they were timed to. Measured on
# 1,000 top songs plus our library (research/search-1000 in the private repo): within a few seconds the first line
# lands on the singer; a longer video (story intro, extra scene) puts every line early.
LYRICS_TOLERANCE = 3.0

def song_name(title: str) -> str:
    """A song's name for matching versions: no "ft. X", "(Remastered)", "- From the film", case or punctuation."""
    title = re.split(r"\s+(?:ft\.?|feat\.?|featuring|with)\s|[(\[]|\s-\s", (title or "").casefold())[0]
    return re.sub(r"\W+", "", title)

def record_song(track: str, artist: str) -> str:
    """The song a record is for, also when it was uploaded with a video's title: "Lady Gaga - Telephone (Official…)"."""
    left, dash, right = (track or "").partition(" - ")
    if dash and lead_artist(left) and lead_artist(left) == lead_artist(artist):
        track = right
    return song_name(track)

def mark_lyrics(results: list[dict], records: list[tuple], tolerance: float = LYRICS_TOLERANCE) -> None:
    """Flag results with synced lyrics of the same song, artist and length.
    records: (song_name, seconds, lead_artist) from LRCLIB (lead_artist optional)."""
    for r in results:
        name = song_name((r.get("parsed") or {}).get("title", "")) or song_name(r.get("title", ""))
        whole = re.sub(r"\W+", "", (r.get("title") or "").casefold())
        text = whole + re.sub(r"\W+", "", f"{r.get('channel') or ''}{(r.get('parsed') or {}).get('artist', '')}".casefold())
        # the song's name, or (for titles like "Adele   Hello" that don't split) found inside the title; and the
        # record's artist in the title, channel or names (a flashmob of "Bohemian Rhapsody" is not Queen's)
        same = lambda track, artist: (track == name or (len(track) >= 4 and track in whole)) and (not artist or artist in text)
        diffs = [abs((r.get("duration") or 0) - rec[1]) for rec in records if rec[0] and same(rec[0], rec[2] if len(rec) > 2 else "")]
        best = min(diffs, default=None)
        # a cover or live version can be as long as the record and still be sung differently: never "matching"
        r["lyrics_match"] = best is not None and best <= tolerance and bool(r.get("duration")) and r.get("kind") not in ("cover", "live")
        if r["lyrics_match"]:
            r["lyrics_diff"] = round(best, 1)

# karaoke-server: names read off a YouTube title were right only ~76% of the time on 1,000 top songs (reversed
# "Song - Artist", the channel taken as the artist, "Audio"/"HQ"/"Remastered 2023" left in). LRCLIB's records for the
# same search carry clean artist and song names; a result that clearly is one of them takes those names.
LABELS = re.compile(r"official|lyrics?\b|\bvideo\b|\baudio\b|\bm/?v\b|\bhd\b|\bhq\b|vevo|- topic|karaoke", re.I)

def lead_artist(artist: str) -> str:
    return re.sub(r"\W+", "", re.split(r",|&| x | and | feat\.? | ft\.? | with ", (artist or "").casefold())[0])

def names_from_records(results: list[dict], records: list[dict]) -> None:
    """records: LRCLIB rows with "artistName"/"trackName". Sets r["parsed"] (keeping the old one as "guessed")."""
    from collections import Counter
    votes = {}
    for c in records:
        name, by = (c.get("trackName") or "").strip(), (c.get("artistName") or "").strip()
        track, artist = song_name(name), lead_artist(by)
        # records uploaded from a video player carry its title ("Ed Sheeran - Shape Of You (Official Video)")
        if len(track) < 2 or len(artist) < 2 or track == artist or " - " in name or LABELS.search(name) or LABELS.search(by) \
                or artist in re.sub(r"\W+", "", name.casefold()):  # "Ed Sheeran-Perfect": the artist glued into the song
            continue
        votes.setdefault(track, {}).setdefault(artist, Counter())[(by, name)] += 1
    # records with artist and song the wrong way round: a "song" that is also an artist's name is not one
    # Which way round is a pair? The search's own YouTube titles mostly read "Artist - Song", so the side most of them
    # put first is the artist (records outvote them only when YouTube can't tell).
    pairs = Counter()
    for r in results:
        parsed = r.get("parsed") or {}
        pairs[(lead_artist(parsed.get("artist", "")), song_name(parsed.get("title", "")))] += 2
    for track, artists in votes.items():
        for artist, spellings in artists.items():
            pairs[(artist, track)] += sum(spellings.values())
    # Only a pair LRCLIB itself has both ways round is in doubt: Filipino uploads often read "Song - Artist", so
    # YouTube's majority alone must not throw out a record ("Kung Kailangan Mo Ako - Rey Valera").
    for track in list(votes):
        for artist in list(votes[track]):
            if artist in votes and track in votes[artist] and pairs[(track, artist)] > pairs[(artist, track)]:
                del votes[track][artist]
        if not votes[track]:
            del votes[track]
    # the spelling most records agree on (album uploads outnumber the odd one), then the shortest
    known = {track: {artist: dict(zip(("artistName", "trackName"), min(spellings, key=lambda k: (-spellings[k], len(k[1])))))
                     for artist, spellings in artists.items()} for track, artists in votes.items()}
    for r in results:
        parsed = r.get("parsed") or {}
        title = re.sub(r"\W+", "", (r.get("title") or "").casefold())
        text = title + re.sub(r"\W+", "", (r.get("channel") or "").casefold())
        exact = {song_name(parsed.get("title", "")), song_name(parsed.get("artist", ""))}  # either way round
        best = None
        for track, artists in known.items():
            if track not in exact and not (len(track) >= 4 and track in title):
                continue
            named = [c for a, c in artists.items() if a in text]
            # the artist must be in the title or channel
            # (an artist is only filled in when the title names none: "Eye of the Tiger" alone)
            chosen = named[0] if len(named) == 1 else next(iter(artists.values())) if not named and len(artists) == 1 \
                and track == song_name(parsed.get("title", "")) and not parsed.get("artist") else None
            if chosen and (best is None or len(track) > len(best[0])):
                best = (track, chosen)
        if best:
            r["parsed"] = {"artist": best[1]["artistName"], "title": best[1]["trackName"], "guessed": parsed}

def order_results(results: list[dict], query: str = "") -> list[dict]:
    """Group by kind (official first), keeping YouTube's order within each kind. karaoke-server: when one song has
    several official videos, the shortest comes first. Long cuts often have a story intro or skits that throw the
    lyrics off (user, 2026-11-21). Different songs keep YouTube's relevance order."""
    def song(r):
        # the song's name alone: no "ft. X", no "(Explicit)", "(Taylor's Version)", "(2003 Edit)"...
        title = (r.get("parsed") or {}).get("title", "").casefold()
        title = re.split(r"\s+(?:ft\.?|feat\.?|featuring)\s|[(\[]", title)[0]
        return re.sub(r"\W+", "", title) or r["id"]
    first = {}
    for i, r in enumerate(results):
        first.setdefault(song(r), i)
    wanted = re.sub(r"\W+", "", (query or "").casefold())
    def key(r):
        # karaoke-server: results whose length matches synced lyrics first (official ones first, then lyric videos...),
        # then the rest as before (covers and live versions are never marked as matching: see mark_lyrics)
        kind = r.get("kind", "other")
        # karaoke-server: the song that was asked for first ("Why lany": LANY's "Why" before LANY's "you!")
        name = song_name((r.get("parsed") or {}).get("title", ""))
        asked = 0 if wanted and len(name) >= 2 and name in wanted else 1
        if r.get("lyrics_match"):
            return (asked, 0, KIND_ORDER[kind], r.get("lyrics_diff", 0), 0)
        if kind != "official":
            return (asked, 1, KIND_ORDER[kind], 0, 0)
        return (asked, 1, 0, first[song(r)], r.get("duration") or float("inf"))
    return sorted(results, key=key)

def _extract(options: dict, video_id: str, progress=None, attempts: int = 4) -> dict:
    """Download with fresh stream URLs on HTTP 403, which YouTube returns intermittently.

    progress(fraction) is called from yt-dlp's download thread.
    """
    import yt_dlp
    if progress:
        def hook(d):
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if d.get("status") == "downloading" and total:
                progress(min(1.0, d.get("downloaded_bytes", 0) / total))
        options = {**options, "progress_hooks": [hook]}
    for attempt in range(attempts):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
        except yt_dlp.utils.DownloadError as error:
            if "403" not in str(error) or attempt == attempts - 1:
                raise
            time.sleep(3 * (attempt + 1))

def download(video_id: str, directory: Path, progress=None) -> dict:
    if not ID.fullmatch(video_id):
        raise ValueError("Invalid YouTube ID")
    directory.mkdir(parents=True, exist_ok=True)
    info = _extract({**base_options(), "format": "bestaudio[ext=m4a]/bestaudio", "outtmpl": str(directory / "audio.%(ext)s"),
                     "noplaylist": True, "quiet": True, "noprogress": True, "socket_timeout": 30,
                     "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]}, video_id, progress)
    return {"duration": info.get("duration"), "source_title": info.get("title", "")}

VIDEO_HEIGHT = int(os.environ.get("KTVIBES_VIDEO_HEIGHT", 720))
BOT_CHECK = re.compile(r"confirm you.re not a bot", re.I)
BOT_HELP = ("YouTube wants this computer to prove it isn't a bot (it happens after many downloads). "
            "Wait a while and Retry, or let KTVibes use your browser's YouTube login: see KTVIBES_COOKIES_FROM_BROWSER in the README.")

def explain(error: Exception) -> str:
    """A yt-dlp error in words a host can act on."""
    return BOT_HELP if BOT_CHECK.search(str(error)) else str(error)[-500:]

def base_options() -> dict:
    """yt-dlp options for every request: Node.js for YouTube's player code, and optionally a browser's
    YouTube cookies (KTVIBES_COOKIES_FROM_BROWSER=chrome), which gets past YouTube's bot check."""
    browser = os.environ.get("KTVIBES_COOKIES_FROM_BROWSER")
    return {"js_runtimes": {"node": {}}, **({"cookiesfrombrowser": (browser,)} if browser else {})}

def download_video(video_id: str, directory: Path, progress=None) -> None:
    """Cache browser-compatible H.264 video with no embedded audio; 720p is about half the size of 1080p."""
    if not ID.fullmatch(video_id):
        raise ValueError("Invalid YouTube ID")
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "video-source.mp4"
    _extract({
        **base_options(),
        "format": f"bestvideo[ext=mp4][vcodec^=avc1][height<={VIDEO_HEIGHT}]/best[ext=mp4][vcodec^=avc1][height<={min(VIDEO_HEIGHT, 720)}]",
        "outtmpl": str(source), "noplaylist": True, "quiet": True, "noprogress": True, "socket_timeout": 30,
    }, video_id, progress)
    temporary = directory / "video.tmp.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", str(source), "-map", "0:v:0", "-an",
                    "-c:v", "copy", "-movflags", "+faststart", str(temporary)],
                   check=True, capture_output=True)
    temporary.replace(directory / "video.mp4")
    source.unlink(missing_ok=True)
    make_30fps(directory)

# karaoke-server: a 60 fps video stays as downloaded (the best source), and gets a 30 fps copy at the same
# resolution, video-30.mp4, made at low CPU priority. A TV that drops frames on the original switches to the copy.
def video_fps(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    try:
        num, _, den = out.partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0

def make_30fps(directory: Path) -> bool:
    """Make (or refresh) video-30.mp4 when video.mp4 runs faster than 30 fps; True if a copy exists afterwards."""
    video, copy = directory / "video.mp4", directory / "video-30.mp4"
    if not video.is_file() or video_fps(video) <= 31:
        copy.unlink(missing_ok=True)
        return False
    if copy.is_file() and copy.stat().st_mtime >= video.stat().st_mtime:
        return True
    temporary = directory / "video-30.tmp.mp4"
    subprocess.run(["nice", "-n", "10", "ffmpeg", "-nostdin", "-y", "-i", str(video), "-an", "-vf", "fps=30",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(temporary)], check=True, capture_output=True)
    temporary.replace(copy)
    return True

if __name__ == "__main__":
    print(json.dumps(search(" ".join(sys.argv[1:]) or "bohemian rhapsody"), ensure_ascii=False, indent=2))
