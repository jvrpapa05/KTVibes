"""karaoke-server: Spotify's catalogue for the remote's search suggestions (user, 2026-11-26).

Official Web API, client-credentials (the app's own keys; nobody logs in). Keys come from the environment
(SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET, set by the systemd unit's EnvironmentFile); without them everything here
quietly does nothing and the remote keeps YouTube's suggestions. Answers are kept in memory for an hour, so typing the
same thing again costs nothing. Spotify only suggests: the karaoke-ready video is still found by our own search.
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MARKET = os.environ.get("SPOTIFY_MARKET", "PH")  # most guests are Filipino: OPM first
_token = {"value": "", "until": 0.0}
_lock = threading.Lock()
_cache: dict[str, tuple[float, list]] = {}
KEEP = 3600

_quiet_until = 0.0  # Spotify said "too many requests": no calls until then (its Retry-After)

class Resting(Exception):
    """Spotify asked us to wait (HTTP 429); callers fall back as if Spotify were off."""

def enabled() -> bool:
    return bool(os.environ.get("SPOTIFY_CLIENT_ID") and os.environ.get("SPOTIFY_CLIENT_SECRET")) and time.time() >= _quiet_until

def _open(request: urllib.request.Request, timeout: float):
    """urlopen that honours Spotify's rate limit: on a 429, stop calling for its Retry-After (asking again during the
    wait can make it longer). karaoke-server, 2026-11-26: a burst of 700 lookups earned a 23-hour wait."""
    global _quiet_until
    if time.time() < _quiet_until:
        raise Resting(f"Spotify asked to wait until {time.ctime(_quiet_until)}")
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            try:
                wait = float(exc.headers.get("Retry-After") or 60)
            except ValueError:
                wait = 60.0
            _quiet_until = time.time() + max(wait, 30)
            raise Resting(f"Spotify asked to wait {wait:.0f} s") from exc
        raise

def _access_token() -> str:
    with _lock:
        if _token["value"] and time.time() < _token["until"] - 60:
            return _token["value"]
        pair = f"{os.environ['SPOTIFY_CLIENT_ID']}:{os.environ['SPOTIFY_CLIENT_SECRET']}".encode()
        request = urllib.request.Request("https://accounts.spotify.com/api/token", data=b"grant_type=client_credentials",
                                         headers={"Authorization": "Basic " + base64.b64encode(pair).decode(),
                                                  "Content-Type": "application/x-www-form-urlencoded"})
        with _open(request, 5) as response:
            answer = json.load(response)
        _token.update(value=answer["access_token"], until=time.time() + float(answer.get("expires_in", 3600)))
        return _token["value"]

def search_tracks(query: str, limit: int = 6) -> list[dict]:
    """Songs for a partial search: [{"title", "artist", "duration", "art", "id"}], best first, one per song."""
    key = f"{MARKET}|{limit}|{query.strip().casefold()}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < KEEP:
        return hit[1]
    # (search returns at most 10 results for an app in development mode; asking for more is refused)
    url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode({"q": query, "type": "track", "limit": min(10, limit * 2), "market": MARKET})
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {_access_token()}"})
    with _open(request, 4) as response:
        items = json.load(response).get("tracks", {}).get("items", [])
    songs, seen = [], set()
    for t in items:
        artists = [a["name"] for a in t.get("artists", [])]
        if not artists or not t.get("name"):
            continue
        same = (t["name"].casefold().split(" - ")[0].split(" (")[0], artists[0].casefold())
        if same in seen:
            continue  # the same song on another album or a deluxe edition
        seen.add(same)
        images = sorted(t.get("album", {}).get("images", []), key=lambda i: i.get("width") or 0)
        art = next((i["url"] for i in images if (i.get("width") or 0) >= 60), images[-1]["url"] if images else "")
        cover = next((i["url"] for i in images if (i.get("width") or 0) >= 300), images[-1]["url"] if images else "")
        songs.append({"title": t["name"], "artist": ", ".join(artists), "duration": round(t.get("duration_ms", 0) / 1000, 1), "art": art,
                      "cover": cover, "id": t["id"], "explicit": bool(t.get("explicit")),
                      "year": (t.get("album", {}).get("release_date") or "")[:4]})
        if len(songs) >= limit:
            break
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.time(), songs)
    return songs

def find_track(artist: str, title: str) -> dict | None:
    """Spotify's song for this artist and title, only when it clearly is this one (the same song name, one of its
    artists named); the exact title first ("Palagi" over "Palagi - TJxKZ Version"). None when not found."""
    import re
    from .youtube import artist_keys, fold, song_name
    want, keys = song_name(title), artist_keys(artist)
    if not want or not keys:
        return None
    plain = lambda text: re.sub(r"\W+", "", fold(text))
    fits = [song for song in search_tracks(f"{title} {artist}", limit=5) if song_name(song["title"]) == want and keys & artist_keys(song["artist"])]
    return next((song for song in fits if plain(song["title"]) == plain(title)), fits[0] if fits else None)

def album_cover(artist: str, title: str) -> str:
    """karaoke-server (user, 2026-11-26): the album cover for a song, for the TV and phones (a YouTube thumbnail is a
    cropped video frame, often with half a title on it). Only when Spotify's song clearly is this one: the same song
    name and one of its artists named; "" otherwise."""
    song = find_track(artist, title)
    return song["cover"] if song else ""
