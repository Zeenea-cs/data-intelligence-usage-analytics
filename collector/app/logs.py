"""Per-service log access for the web UI.

Each stack service writes its log to a file in the shared log directory
(bind-mounted into every container); the web UI tails these files on demand. A
fixed service -> filename map both labels the UI picker and prevents path
traversal (only these names are ever opened).
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

# UI service name -> log file written into the shared log directory.
SERVICE_LOG_FILES: dict[str, str] = {
    "collector": "collector.log",
    "db": "db.log",
    "metabase": "metabase.log",
    "metabase-db": "metabase-db.log",
}

# Bound on how many lines a single tail request may return.
MAX_TAIL_LINES = 2000


def available_services(log_dir: str) -> list[str]:
    """Return the services whose log file currently exists in `log_dir`."""
    base = Path(log_dir)
    return [name for name, fn in SERVICE_LOG_FILES.items() if (base / fn).is_file()]


def tail_log(log_dir: str, service: str, lines: int) -> str | None:
    """Return the last `lines` lines of a service's log, or None if unavailable.

    Only the known service names map to a file, so arbitrary paths can never be
    read. `lines` is clamped to [1, MAX_TAIL_LINES].
    """
    filename = SERVICE_LOG_FILES.get(service)
    if filename is None:
        return None
    path = Path(log_dir) / filename
    if not path.is_file():
        return None
    count = max(1, min(lines, MAX_TAIL_LINES))
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        # deque(maxlen) keeps only the last `count` lines without loading all.
        return "".join(deque(handle, maxlen=count))
