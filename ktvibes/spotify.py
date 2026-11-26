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
import urllib.parse
import urllib.request

MARKET = os.environ.get("SPOTIFY_MARKET", "PH")  # most guests are Filipino: OPM first
_token = {"value": "", "until": 0.0}
_lock = threading.Lock()
_cache: dict[str, tuple[float, list]] = {}
KEEP = 3600

def enabled() -> bool:
    return bool(os.environ.get("SPOTIFY_CLIENT_ID") and os.environ.get("SPOTIFY_CLIENT_SECRET"))

def _access_token() -> str:
    with _lock:
        if _token["value"] and time.time() < _token["until"] - 60:
            return _token["value"]
        pair = f"{os.environ['SPOTIFY_CLIENT_ID']}:{os.environ['SPOTIFY_CLIENT_SECRET']}".encode()
        request = urllib.request.Request("https://accounts.spotify.com/api/token", data=b"grant_type=client_credentials",
                                         headers={"Authorization": "Basic " + base64.b64encode(pair).decode(),
                                                  "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(request, timeout=5) as response:
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
    with urllib.request.urlopen(request, timeout=4) as response:
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
        songs.append({"title": t["name"], "artist": ", ".join(artists), "duration": round(t.get("duration_ms", 0) / 1000, 1), "art": art, "id": t["id"]})
        if len(songs) >= limit:
            break
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.time(), songs)
    return songs
