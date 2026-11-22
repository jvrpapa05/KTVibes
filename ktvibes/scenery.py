"""karaoke-server (user, 2026-11-22): calm scenery instead of a lyric video's own (clashing) words.

Long nature/drone films live in <cache>/scenery/<id>.mp4 (scripts/fetch-scenery.sh in the server repo cuts each film's
intro off when downloading). Each song that needs scenery gets the next unused stretch, as long as the song; a pointer
saved in scenery.json carries on across songs, restarts and parties, so footage is never shown twice until every film
has been used up. EDGE seconds are left at each end of a film (cuts land on keyframes, and films fade at the ends).
"""
import json
import logging
import re
import subprocess
from pathlib import Path

EDGE = 8.0
NAME = re.compile(r"[A-Za-z0-9_-]{11}\.mp4")

class Scenery:
    def __init__(self, folder: Path):
        self.folder = folder
        self.path = folder / "scenery.json"

    def _state(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict):
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(self.path)

    def films(self, state: dict) -> list[tuple[str, float]]:
        """(file name, seconds) of every finished film, in a fixed order; durations are measured once."""
        lengths = state.setdefault("lengths", {})
        out = []
        for path in sorted(self.folder.glob("*.mp4")) if self.folder.is_dir() else []:
            if not NAME.fullmatch(path.name):
                continue  # still downloading (*.part.mp4)
            if path.name not in lengths:
                try:
                    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                           capture_output=True, text=True, timeout=30)
                    lengths[path.name] = float(probe.stdout.strip())
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    continue
            out.append((path.name, lengths[path.name]))
        return out

    def take(self, seconds: float) -> dict | None:
        """The next unused stretch of footage at least `seconds` long: {"file", "start"}; None without films."""
        state = self._state()
        films = [(name, length) for name, length in self.films(state) if length - 2 * EDGE >= seconds]
        if not films:
            self._save(state)
            return None
        names = [name for name, _ in films]
        at = state.get("next") or {}
        index = names.index(at["file"]) if at.get("file") in names else 0
        start = max(EDGE, float(at.get("at") or 0)) if at.get("file") in names else EDGE
        for _ in range(len(films) + 1):
            name, length = films[index]
            if start + seconds <= length - EDGE:
                break
            index, start = (index + 1) % len(films), EDGE
            if index == 0:
                logging.getLogger("uvicorn.error").info("Scenery: every film has been shown; starting over")
        state["next"] = {"file": name, "at": round(start + seconds + 1, 2)}
        self._save(state)
        return {"file": name, "start": round(start, 2)}
