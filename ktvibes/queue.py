"""All state mutations run on the event loop; queue entries have unique identities."""
import asyncio
import json
import logging
import math
import os
from pathlib import Path
import re
import time
import uuid
from . import songbook, metrics

DISPLAY = {"video_mode": ("show", "blur", "hide"), "lyric_mode": ("two", "scroll", "off"), "theme": ("default",)}  # karaoke-server: Classic theme removed (English-only users)
def following(options, current):
    """The option after `current`, wrapping round: what a "cycle" control switches to."""
    return options[(options.index(current) + 1) % len(options)]

GUIDES = ("off", "latin", "jyutping", "hangul")
REACTIONS = ("👏", "🔥", "🎉", "💚")  # cheers a remote can float up on the TV

def one_emoji(value):
    """karaoke-server: a phone's own cheer must be a single emoji (skin tones, flags, keycaps and ZWJ sequences
    like 👨‍🎤 included), never plain text, so the cheer button can't put words on the TV."""
    import unicodedata
    if not isinstance(value, str) or not 1 <= len(value) <= 12:
        return False
    joiners = {0x200D, 0xFE0E, 0xFE0F, 0x20E3}
    pictures = 0
    for ch in value:
        cp = ord(ch)
        if cp in joiners or 0xE0020 <= cp <= 0xE007F or 0x1F3FB <= cp <= 0x1F3FF:
            continue  # joiner, variation selector, keycap, flag tag, skin tone
        if unicodedata.category(ch) in ("So", "Sk") or 0x1F1E6 <= cp <= 0x1F1FF:
            pictures += 1
            continue
        if ch in "#*0123456789" and "\u20e3" in value:
            pictures += 1
            continue  # keycap base, e.g. 1️⃣
        return False
    return 1 <= pictures <= 4
# karaoke-server: shout-outs (short text) and photos that pop up on the TV, always with the sender's singer name.
FUN_COOLDOWN = 10  # seconds between shout-outs from one phone
PHOTO_COOLDOWN = 3  # seconds between photos from one phone: a burst of photos piles up on the TV
SHOUT_MAX = 60  # characters
PHOTO_MAX_BYTES = 1_000_000  # phones shrink photos to ~150 KB before sending
PHOTO_KEEP = 12 * 3600  # photos live in memory only, for the night
PHOTO_LIMIT = 60
# karaoke-server: our own SemVer, hand-maintained in ktvibes/VERSION and bumped with every change (upstream's
# version in pyproject.toml is left alone so merges stay clean). Shown faintly on the TV and the remote.
VERSION = (Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip() if Path(__file__).with_name("VERSION").is_file() else "dev")
SINGER_IDLE = 6 * 3600  # karaoke-server: a singer name is freed after 6 idle hours
# karaoke-server: every room setting a singer can set is part of their profile. Not the song position, and not the
# lyric timing nudge (offset): that corrects the song itself and is kept per song in meta.json.
PROFILE = ("vocal", "music", "key", "speed", "lyric_scale", "video_mode", "lyric_mode")
HISTORY = 200  # remembered plays, for the remote's "Recent" list
UNDO_SECONDS = 15  # how long a removal can be taken back
SAVE_EVERY = 10  # seconds between queue.json writes for position reports alone
BREATHER = 4  # seconds between one song ending and the next starting
# karaoke-server: "Were the lyrics in sync?" after each song (user, 2026-11-22). Everyone can vote; the countdown is
# VOTE_WAIT and drops to VOTE_FAST once every singer with a song in the queue has voted. Voting stays open until
# VOTE_AFTER seconds into the next song. Songs stopped before VOTE_MIN_PLAYED aren't asked about.
VOTE_WAIT = 10
VOTE_FAST = 3
VOTE_AFTER = 20  # (the TV's tally) seconds into the next song
VOTE_KEEP = 2 * 3600  # at most this long, even if the next song never ends (normally a song's question closes when the next one ends)
VOTE_MIN_PLAYED = float(os.environ.get("KTVIBES_VOTE_MIN_PLAYED", 30))  # tests set it lower
VOTE_WHY = ("early", "late", "words")
SCORE_HOLD = 5  # Classic: the score shows this long when a song is sung to the end, before the 예약곡 board
ROOM_MINUTES = 30  # a Classic room starts with this much singing time

def number(value) -> float:
    """Control values arrive as JSON; reject text and NaN/inf before clamping."""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Expected a finite number")
    return result


class State:
    def __init__(self):
        self.current = None
        self.upcoming = []
        self.clients = {}
        self.player = None
        self.playing = True
        self.offset = 0.0
        self.vocal = 0.1
        self.music = 1.0  # instrumental (backing track) volume
        self.guide = "off"  # karaoke-server: pronunciation guides removed (English-only users); always off
        self.lyric_scale = 1.5
        self.video_mode = "show"  # show | blur | hide: blur suits lyric videos, whose own lyrics clash
        self.lyric_mode = "two"  # two (classic two-line KTV) | scroll | off
        self.theme = "default"  # default | classic (Korean noraebang look, with a room timer)
        self.room_ends = None  # unix time a Classic room's singing time runs out
        self.speed = 1.0  # playback tempo, 0.5-1.5; pitch is kept
        self.key = 0  # pitch shift in semitones, -6..+6
        self.seek_id = 0
        self.position = 0.0
        self.transition_until = 0.0
        self.wake = asyncio.Event()
        self.revision = 0
        self.broadcast_lock = asyncio.Lock()
        self.path = None  # queue.json; set by restore() so the queue survives restarts
        self.played = {}  # video id -> unix time the song last went on stage
        self.audio = False  # the TV has enabled sound, so playback can actually start
        self.undo = None  # the last removal, for the remote's Undo toast
        self.singers = {}  # karaoke-server: name key -> {"name", "device", "seen"}; one phone per name at a time
        self.profiles = {}  # karaoke-server: name key -> {setting: value for PROFILE}, applied on that singer's songs
        self.fun_last = {}  # karaoke-server: device -> unix time of its last shout-out or photo
        self.photos = {}  # karaoke-server: photo id -> {"data", "type", "name", "at"}; memory only, never saved
        self.saved_at = 0.0
        self.build = ""  # fingerprint of the web app files; open pages reload when it changes
        self.lyrics_sent = {}  # TV socket -> key of the song whose lyrics it already has
        self.lyrics_applying = {}  # karaoke-server: song id -> {"version", "started"} while new lyrics are timed in the background
        self.vote = None  # karaoke-server: the open "lyrics in sync?" vote (see VOTE_WAIT)

    def guides(self):
        """Guide modes with something to show for the current song (all of them when idle)."""
        return ["off"]  # karaoke-server: English-only users, no pronunciation guides
        units = [u for line in (self.current or {}).get("lyrics", []) for u in line.get("units") or []]
        if not self.current:
            return list(GUIDES)
        return ["off"] + [mode for index, mode in ((3, "latin"), (5, "jyutping"), (4, "hangul")) if any(len(u) > index and u[index] for u in units)]

    def time_up(self):
        return self.room_ends is not None and time.time() >= self.room_ends

    def snapshot(self, lyrics=True):
        """Lyrics are large and only the TV draws them, so broadcasts leave them out (see broadcast)."""
        brief = lambda item: item if lyrics or item is None else {k: v for k, v in item.items() if k != "lyrics"}
        return {"current": brief(self.current), "upcoming": [brief(i) for i in self.upcoming], "playing": self.playing,
                "offset": self.offset, "vocal": self.vocal, "music": self.music, "guide": self.guide, "guides": self.guides(), "lyric_scale": self.lyric_scale, "video_mode": self.video_mode, "lyric_mode": self.lyric_mode, "theme": self.theme, "room_ends": self.room_ends, "time_up": self.time_up(), "profiles": self.profiles, "speed": self.speed, "key": self.key, "seek_id": self.seek_id, "position": self.position,
                "transition_until": self.transition_until, "server_time": time.time(),
                "player_connected": self.player is not None, "player_audio": self.player is not None and self.audio,
                "revision": self.revision, "build": self.build, "version": VERSION, "phones": sum(role == "remote" for role in self.clients.values()),
                "lyrics_applying": {k: {"version": v["version"], "started": v["started"]} for k, v in self.lyrics_applying.items()},
                "vote": self.vote_summary()}

    # karaoke-server: the lyrics vote ------------------------------------------------------------------------------
    def vote_voters(self):
        """Phones whose singer has a song in the queue (or just sang): the room waits for them."""
        names = {str(i.get("singer") or "").casefold() for i in (self.vote or {}).get("song", []) + [self.current, *self.upcoming] if i}
        return {self.singers[n]["device"] for n in names if n and n in self.singers}

    def vote_summary(self):
        vote = self.vote
        if not vote or time.time() > vote["keep"]:
            return None
        votes = vote["votes"].values()
        needed = self.vote_voters()
        return {"key": vote["key"], "id": vote["id"], "title": vote["title"], "artist": vote["artist"], "singer": vote["singer"],
                "up": sum(v == "up" for v in votes), "down": sum(v == "down" for v in votes),
                "needed": len(needed), "voted_needed": len(needed & set(vote["votes"])), "closes": vote["closes"]}

    def open_vote(self, item, position):
        if position < VOTE_MIN_PLAYED or not item.get("lyrics"):
            self.vote = None  # not asked about; the song before is no longer voted on either
            return
        now = time.time()
        waiting = bool(self.current or any(i["status"] != "error" for i in self.upcoming))
        if waiting:
            self.transition_until = max(self.transition_until, now + VOTE_WAIT)
        closes = (self.transition_until if waiting else now + VOTE_WAIT) + VOTE_AFTER
        self.vote = {"key": item["key"], "id": item["id"], "title": item["title"], "artist": item["artist"], "singer": item.get("singer") or "",
                     "song": [item], "votes": {}, "why": {}, "opened": now, "closes": closes, "keep": now + VOTE_KEEP}

    def cast_vote(self, message):
        """One vote per phone; a phone may change its vote. Returns the stored record for the song's meta."""
        device = str(message.get("device") or "")
        # user (2026-11-22): song A can be voted on while song B plays; once B ends, only B can
        vote = self.vote if self.vote and message.get("key") == self.vote["key"] else None
        if not vote or time.time() > vote["keep"]:
            raise ValueError("Voting for that song has closed")
        if len(device) < 8:
            raise ValueError("This remote needs a reload")
        value = message.get("value")
        if value not in ("up", "down"):
            raise ValueError("Vote up or down")
        why = message.get("why")
        if why is not None and why not in VOTE_WHY:
            raise ValueError("Unknown reason")
        vote["votes"][device] = value
        if value == "up":
            vote["why"].pop(device, None)
        elif why:
            vote["why"][device] = why
        # everyone the room was waiting for has voted: start the next song soon
        needed = self.vote_voters()
        if needed and needed <= set(vote["votes"]) and self.transition_until - time.time() > VOTE_FAST:
            self.transition_until = time.time() + VOTE_FAST
        metrics.record("lyrics_vote", song=vote["id"], title=vote["title"], vote=value, why=why,
                       singer=device == (self.singers.get(vote["singer"].casefold()) or {}).get("device"), needed=device in needed)
        votes = list(vote["votes"].values())
        return {"key": vote["key"], "at": round(vote["opened"]), "up": votes.count("up"), "down": votes.count("down"),
                "why": {w: list(vote["why"].values()).count(w) for w in VOTE_WHY if w in vote["why"].values()}}

    def save_vote(self, video_id, record):
        """karaoke-server: keep each song's votes in its meta.json, tied to the lyrics they were about (lyrics.lrc)."""
        if not self.path:
            return
        folder = self.path.parent / video_id
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            lrc = (folder / "lyrics.lrc").read_text(encoding="utf-8")
        except (OSError, ValueError):
            return
        import hashlib
        record = {**record, "lyrics": hashlib.sha256(lrc.encode()).hexdigest()[:16]}
        votes = [v for v in meta.get("lyrics_votes", []) if v.get("key") != record["key"]] + [record]
        meta["lyrics_votes"] = votes[-50:]
        temporary = folder / "meta.vote.tmp"
        temporary.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        temporary.replace(folder / "meta.json")

    def restore(self, path: Path, seed=lambda: {}):
        """Reload the saved queue; songs are prepared again (fast from cache) and the current one resumes."""
        self.path = path
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        # Before play history existed, fall back to whatever seed() knows (e.g. cache timestamps).
        self.played = saved["played"] if isinstance(saved.get("played"), dict) else seed()
        self.theme = saved.get("theme") if saved.get("theme") in DISPLAY["theme"] else "default"
        self.singers = saved["singers"] if isinstance(saved.get("singers"), dict) else {}
        self.profiles = saved["profiles"] if isinstance(saved.get("profiles"), dict) else {}
        self.room_ends = float(saved["room_ends"]) if self.theme == "classic" and isinstance(saved.get("room_ends"), (int, float)) else None
        entries = saved.get("upcoming", [])
        if saved.get("current"):
            entries = [saved["current"], *entries]
        for entry in entries:
            item = self.entry(entry["id"], entry["artist"], entry["title"], entry.get("singer", ""))
            if entry is saved.get("current"):
                item["resume"] = float(entry.get("position") or 0)
            self.upcoming.append(item)
        self.wake.set()

    def save(self):
        if self.path is None:
            return
        brief = lambda i: {"id": i["id"], "artist": i["artist"], "title": i["title"], "singer": i.get("singer", "")}
        data = {"current": self.current and {**brief(self.current), "position": self.position},
                "upcoming": [brief(i) for i in self.upcoming if i["status"] != "error"], "played": self.played,
                "theme": self.theme, "room_ends": self.room_ends, "singers": self.singers, "profiles": self.profiles}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    async def broadcast(self, persist=True):
        async with self.broadcast_lock:
            self.revision += 1
            if persist:
                try:
                    self.save()
                    self.saved_at = time.monotonic()
                except OSError:
                    pass  # a full disk should not stop the party
            message = {"type": "state", **self.snapshot(lyrics=False)}
            key = self.current and self.current["key"]
            async def deliver(ws):
                # A TV gets the current song's lyrics once per song (and again after reconnecting).
                out = message
                if key and self.clients.get(ws) == "tv" and self.lyrics_sent.get(ws) != key:
                    out = {**message, "current": {**message["current"], "lyrics": self.current["lyrics"]}}
                if await self.send(ws, out):
                    self.lyrics_sent[ws] = key
            await asyncio.gather(*(deliver(ws) for ws in list(self.clients)))

    async def send(self, ws, message):
        """Send to one client; a socket that can't take it within 2 s is dropped."""
        try:
            await asyncio.wait_for(ws.send_json(message), timeout=2)
            return True
        except Exception:
            self.disconnect(ws)
            return False

    def disconnect(self, ws):
        self.clients.pop(ws, None)
        self.lyrics_sent.pop(ws, None)
        if self.player is ws:
            self.player = None
            self.audio = False

    def duration_sung(self):
        """karaoke-server: seconds into the song on stage (position as last reported by the TV)."""
        return float(self.position or 0)

    def advance(self, scored=False):
        self.current = None
        self.position = 0
        self.offset = 0
        # Tempo and key suit one singer and one song, so each song starts as recorded.
        # (karaoke-server: promote() then applies the next singer's saved profile, if any.)
        self.speed = 1.0
        self.key = 0
        # A short breather between songs shows who's up next; a song on an empty stage starts at once.
        self.transition_until = time.time() + BREATHER + (SCORE_HOLD if scored and self.theme == "classic" else 0)
        self.promote()

    def promote(self):
        if self.time_up():
            return  # the song on stage finishes; the next waits for more time
        if self.current is None:
            candidate = next((item for item in self.upcoming if item["status"] != "error"), None)
            if candidate and candidate["status"] == "ready":
                self.upcoming.remove(candidate)
                self.current = candidate
                self.position = candidate.pop("resume", 0)
                self.played[candidate["id"]] = time.time()
                if len(self.played) > HISTORY:
                    self.played = dict(sorted(self.played.items(), key=lambda p: p[1])[-HISTORY:])
                self.offset = candidate.get("offset", 0)
                self.apply_profile()
                # Cantopop reads in jyutping, Mandarin in pinyin: follow the song when on either.
                if self.guide in ("latin", "jyutping"):
                    from .lyrics import cantonese
                    self.guide = "jyutping" if cantonese(candidate.get("lyrics") or []) and "jyutping" in self.guides() else "latin"

    @staticmethod
    def entry(video_id, artist, title, singer=""):
        return {"key": uuid.uuid4().hex, "id": video_id, "artist": artist, "title": title, "singer": singer, "added": time.time(),
                "status": "queued", "lyrics": [], "duration": 0, "error": None}

    def apply_profile(self):
        """karaoke-server: put the current singer's saved settings on stage (e.g. Anna 20% vocals, Bea 50%)."""
        singer = (self.current or {}).get("singer")
        profile = self.profiles.get(singer.casefold()) if singer else None
        for name in PROFILE if profile else ():
            if name in profile:
                setattr(self, name, profile[name])

    def remember_profile(self):
        """karaoke-server: a change made during someone's song becomes that singer's profile."""
        singer = (self.current or {}).get("singer")
        if singer:
            self.profiles[singer.casefold()] = {name: getattr(self, name) for name in PROFILE}

    def remember_offset(self):
        """Keep the lyric timing nudge in the song's meta.json, so it applies next time too."""
        if self.path is None or not self.current:
            return
        self.current["offset"] = self.offset
        meta_path = self.path.parent / self.current["id"] / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["lyric_offset"] = self.offset
            temporary = meta_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(meta_path)
        except (OSError, ValueError):
            pass

    def take_back(self):
        """Put the last removed song back where it was, while the removal is still recent."""
        undo, self.undo = self.undo, None
        if not undo or time.time() > undo["until"]:
            raise ValueError("Nothing to undo")
        if self.queued(undo["item"]["id"]):
            raise ValueError("That song is already in the queue")
        self.upcoming.insert(min(undo["index"], len(self.upcoming)), undo["item"])

    def queued(self, video_id):
        return any(i and i["id"] == video_id and i["status"] != "error" for i in (self.current, *self.upcoming))

    def claim_singer(self, name, device):
        """karaoke-server: names are unique. A name belongs to one phone (device id) until it has been idle for
        SINGER_IDLE seconds; then anyone, including the same person on a new phone, can take it. Returns the tidy name."""
        name = " ".join(str(name).split())[:40]
        if not name:
            raise ValueError("Enter your name")
        if not device:
            raise ValueError("This remote needs a reload")
        now = time.time()
        self.singers = {k: v for k, v in self.singers.items() if now - v.get("seen", 0) < SINGER_IDLE}
        key = name.casefold()
        holder = self.singers.get(key)
        if holder and holder["device"] != device:
            raise ValueError(f"“{holder['name']}” is already singing here. Add an initial or a nickname.")
        # One name per phone: switching names frees the old one.
        self.singers = {k: v for k, v in self.singers.items() if v["device"] != device or k == key}
        self.singers[key] = {"name": name, "device": device, "seen": now}
        return name

    async def add(self, video_id, artist, title, singer=""):
        if self.queued(video_id):
            raise ValueError("That song is already in the queue")
        item = self.entry(video_id, artist, title, singer)
        self.upcoming.append(item)
        self.wake.set()
        await self.broadcast()
        return item

    def own_profile_edit(self, message):
        """karaoke-server: each phone edits its own singer's profile. True when the sender is a named singer and the
        stage isn't theirs (someone else's song, or nothing playing): then the change is saved for later and the room
        is left alone. On their own song, or on a song without a name, changes apply live as usual."""
        name = message.get("singer")
        if not name or message.get("action") not in PROFILE:
            return False
        holder = self.singers.get(str(name).casefold())
        if not holder or holder["device"] != message.get("device"):
            raise ValueError("Your name needs a refresh: reload the remote")
        on_stage = (self.current or {}).get("singer") or ""
        return not self.current or (bool(on_stage) and on_stage.casefold() != str(name).casefold())

    def setting(self, action, value):
        """The checked value of one profile setting, with the same limits as the live controls."""
        if action == "speed":
            return round(max(0.5, min(1.5, number(value))), 2)
        if action == "key":
            return int(max(-6, min(6, round(number(value)))))
        if action in ("vocal", "music"):
            return max(0, min(1, number(value)))
        if action == "lyric_scale":
            return max(0.6, min(2.5, number(value)))
        if value not in DISPLAY[action]:
            raise ValueError(f"Unknown {action.replace('_', ' ')}")
        return value

    def sender_name(self, singer, device):
        """karaoke-server: the singer name a phone holds; fun posts always carry it."""
        holder = self.singers.get(" ".join(str(singer or "").split()).casefold())
        if not holder or not device or holder["device"] != device:
            raise ValueError("Pick your name on the remote first")
        holder["seen"] = time.time()
        return holder["name"]

    def fun_allowed(self, device, kind="shout"):
        """One shout-out per FUN_COOLDOWN and one photo per PHOTO_COOLDOWN, per phone (counted separately)."""
        now = time.time()
        key = f"{kind}:{device}"
        wait = (PHOTO_COOLDOWN if kind == "photo" else FUN_COOLDOWN) - (now - self.fun_last.get(key, 0))
        if wait > 0:
            raise ValueError(f"Wait {int(wait) + 1} s before sending another")
        self.fun_last[key] = now

    async def show_fun(self, kind, name, **extra):
        """Pop a shout-out or photo up on the TV, and let every remote offer to hide it."""
        item = {"type": kind, "id": uuid.uuid4().hex[:12], "name": name, **extra}
        metrics.record(kind, singer=name, chars=len(extra.get("text", "")) if kind == "shout" else None)  # never the text itself
        if self.player is not None:
            await self.send(self.player, item)
        notice = {"type": "fun", "kind": kind, "id": item["id"], "name": name}
        await asyncio.gather(*(self.send(ws, notice) for ws, role in list(self.clients.items()) if role != "tv"))
        return item

    async def add_photo(self, data, kind, singer, device, taken=""):
        name = self.sender_name(singer, device)
        if len(data) > PHOTO_MAX_BYTES:
            raise ValueError("That photo is too big")
        self.fun_allowed(device, "photo")
        now = time.time()
        self.photos = {k: v for k, v in self.photos.items() if now - v["at"] < PHOTO_KEEP}
        while len(self.photos) >= PHOTO_LIMIT:
            self.photos.pop(next(iter(self.photos)))
        photo_id = uuid.uuid4().hex
        self.photos[photo_id] = {"data": data, "type": kind, "name": name, "at": now}
        metrics.record("photo_upload", singer=name, bytes=len(data), has_date=bool(taken))  # never the photo itself
        return await self.show_fun("photo", name, photo=photo_id, date=taken)

    async def control(self, message, sender=None):
        action = message.get("action")
        if action == "tv_stats":
            # karaoke-server: the TV's 30-second playback report (resolution, dropped frames, waits for data,
            # jumps, drift, browser), so video lag can be traced. One log line; nothing else changes.
            value = message.get("value")
            if sender is self.player and isinstance(value, dict):
                keep = {k: value[k] for k in ("song", "height", "dropped", "frames", "waits", "jumps", "max_drift", "agent") if k in value}
                logging.getLogger("uvicorn.error").info("TV playback %s", json.dumps(keep, ensure_ascii=False)[:600])
                metrics.record("tv_playback", **keep)
            return
        if action == "tv_event":
            # karaoke-server: things the TV decides by itself (e.g. switching a 60 fps video to its 30 fps copy)
            value = message.get("value")
            if sender is self.player and isinstance(value, dict):
                metrics.record("tv_" + str(value.get("kind", "event"))[:30], **{k: v for k, v in value.items() if k != "kind" and isinstance(v, (int, float, str, bool))})
            return
        if action == "shout":
            name = self.sender_name(message.get("singer"), message.get("device"))
            text = " ".join(str(message.get("value") or "").split())[:SHOUT_MAX]
            if not text:
                raise ValueError("Type a message")
            self.fun_allowed(message.get("device"), "shout")
            await self.show_fun("shout", name, text=text)
            return
        if action == "lyrics_vote":
            record = self.cast_vote(message)
            self.save_vote(self.vote["id"], record)
            await self.broadcast(persist=False)
            return
        if action == "hide":
            # Any phone can take a shout-out or photo off the TV.
            if self.player is not None:
                await self.send(self.player, {"type": "hide", "id": str(message.get("id", ""))[:32]})
            return
        if self.own_profile_edit(message):
            key = str(message["singer"]).casefold()
            # A first-timer's profile starts from the room as it is now.
            profile = {**{n: getattr(self, n) for n in PROFILE}, **self.profiles.get(key, {})}
            profile[action] = self.setting(action, message.get("value"))
            self.profiles[key] = profile
            await self.broadcast()
            return
        if action in ("ended", "progress"):
            if sender is not self.player or not self.current or message.get("key") != self.current["key"]:
                return
        if action == "react":
            # A cheer goes straight to the TV and changes nothing, so there is nothing to save or broadcast.
            if message.get("value") not in REACTIONS and not one_emoji(message.get("value")):  # karaoke-server: own emoji too
                raise ValueError("Unknown reaction")
            if self.player is not None:
                await self.send(self.player, {"type": "react", "value": message["value"]})
            metrics.record("cheer", emoji=message["value"], custom=message["value"] not in REACTIONS)
            return
        if action == "dial":
            # Keypad digits go straight to the TV to show and read out; nothing changes.
            digits = message.get("digits")
            if not isinstance(digits, str) or not re.fullmatch(r"\d{0,5}", digits):
                raise ValueError("Expected up to 5 digits")
            if self.player is not None:
                await self.send(self.player, {"type": "dial", "digits": digits})
            return
        if action == "reserve":
            wanted = int(number(message.get("number")))
            found = self.path and songbook.find(self.path.parent, wanted)
            if not found:
                raise ValueError(f"No song {wanted}")
            if len(self.upcoming) >= 100:
                raise ValueError("Queue is full")
            video_id, meta = found
            item = await self.add(video_id, meta["artist"], meta["title"])
            item["number"] = wanted
            # karaoke-server: no 우선예약 (jump to the front); the queue plays in order.
            if self.player is not None:
                await self.send(self.player, {"type": "reserved", "number": wanted})
            return
        if action == "play":
            self.playing = True
        elif action == "pause":
            self.playing = False
        elif action == "skip" or action == "ended":
            ended, played = self.current, (self.duration_sung() if action == "skip" else float((self.current or {}).get("duration") or 0))
            if self.current:  # karaoke-server metric: finished or skipped, and how far in
                metrics.record("song_played", song=self.current["id"], title=self.current["title"], singer=self.current.get("singer", ""),
                               completed=action == "ended", position=round(self.position), duration=round(self.current.get("duration") or 0),
                               speed=self.speed, key=self.key, vocal=round(self.vocal, 2))
            self.advance(scored=action == "ended")
            if ended:
                self.open_vote(ended, played)
        elif action == "undo":
            self.take_back()
        elif action == "audio":
            if sender is not self.player:
                return
            self.audio = message.get("value") is True
        elif action == "offset":
            self.offset = max(-30, min(30, self.offset + number(message.get("delta", 0))))
            self.remember_offset()
        elif action == "seek":
            # karaoke-server: like a karaoke machine, a song can only be paused or skipped, never moved within.
            raise ValueError("Songs can only be paused or skipped")
        elif action == "lyric_scale":
            self.lyric_scale = max(0.6, min(2.5, number(message["value"])))
            self.remember_profile()
        elif action in DISPLAY:
            options = DISPLAY[action]
            value = message.get("value")
            if value == "cycle":
                value = following(options, getattr(self, action))
            if value not in options:
                raise ValueError(f"Unknown {action.replace('_', ' ')}")
            if action == "lyric_mode" and self.theme == "classic":
                value = "two"  # Classic always shows two lines (a TV restoring its own setting must not fail)
            setattr(self, action, value)
            if action in PROFILE:
                self.remember_profile()
            if action == "theme":
                if value == "classic":
                    self.lyric_mode = "two"
                # Switching to Classic opens a room; leaving it ends the room.
                self.room_ends = (self.room_ends or time.time() + ROOM_MINUTES * 60) if value == "classic" else None
        elif action == "add_time":
            if self.theme != "classic":
                raise ValueError("Room time is part of the Classic theme")
            minutes = int(number(message.get("minutes")))
            if not 1 <= minutes <= 120:
                raise ValueError("Add between 1 and 120 minutes")
            self.room_ends = max(time.time(), self.room_ends or 0) + minutes * 60
        elif action == "speed":
            self.speed = round(max(0.5, min(1.5, number(message["value"]))), 2)
            self.remember_profile()
        elif action == "key":
            self.key = int(max(-6, min(6, round(number(message["value"])))))
            self.remember_profile()
        elif action == "guide":
            return  # karaoke-server: guides are always off; ignore (TVs may still restore an old saved guide)
            modes = self.guides()
            if message.get("value") == "cycle":
                # From a mode this song lacks, jump to its first visible guide rather than "off".
                self.guide = following(modes, self.guide) if self.guide in modes else modes[min(1, len(modes) - 1)]
            elif message.get("value") in GUIDES:
                self.guide = message["value"]
            else:
                raise ValueError("Unknown guide")
        elif action == "music":
            self.music = max(0, min(1, number(message["value"])))
            self.remember_profile()
        elif action == "vocal":
            self.vocal = max(0, min(1, number(message["value"])))
            self.remember_profile()
        elif action == "remove":
            # karaoke-server: the queue is just the list of songs to play; nobody removes songs from it.
            raise ValueError("Songs can't be removed from the queue")
        elif action == "retry":
            item = next((i for i in self.upcoming if i["key"] == message.get("key") and i["status"] == "error"), None)
            if item is None:
                return
            if self.queued(item["id"]):
                raise ValueError("That song is already in the queue")
            item.update(status="queued", error=None, step=None, progress=None)
        elif action == "reorder":
            # karaoke-server: first come, first served. Songs can't be moved (removed songs can still be undone).
            raise ValueError("The queue plays in order: songs can't be moved")
        elif action == "progress":
            self.position = max(0, min(number(message["position"]), self.current["duration"]))
        else:
            raise ValueError("Unknown control")
        self.promote()
        self.wake.set()
        # Position reports arrive every two seconds; saving them occasionally is enough to resume after a restart.
        await self.broadcast(persist=action != "progress" or time.monotonic() - self.saved_at >= SAVE_EVERY)
