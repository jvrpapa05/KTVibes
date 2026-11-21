"""karaoke-server: local usage and health metrics, one JSON object per line in <cache>/metrics.jsonl.

Every event has "t" (unix time), "v" (app version) and "event"; the other fields depend on the event. Nothing is sent
anywhere: the file stays on this server, for analysing parties and planning fixes (see the private repo's
scripts/metrics-report.py). Shout-out text and photos are never recorded, only their size. The file rotates at
METRICS_MAX_BYTES (one previous file kept as metrics.jsonl.1).
"""
import json
import os
import threading
import time
from pathlib import Path

METRICS_MAX_BYTES = 50 * 1024 * 1024
_lock = threading.Lock()
_path: Path | None = None
_version = "dev"


def setup(cache: Path, version: str) -> None:
    global _path, _version
    _path, _version = Path(cache) / "metrics.jsonl", version


def record(event: str, **fields) -> None:
    """Append one event. Never raises: metrics must not break the karaoke."""
    if _path is None:
        return
    line = json.dumps({"t": round(time.time(), 3), "v": _version, "event": event, **fields}, ensure_ascii=False, default=str)
    try:
        with _lock:
            if _path.exists() and _path.stat().st_size > METRICS_MAX_BYTES:
                os.replace(_path, _path.with_name(_path.name + ".1"))
            with open(_path, "a", encoding="utf-8") as out:
                out.write(line + "\n")
    except OSError:
        pass
