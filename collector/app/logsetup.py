"""Logging configuration for the collector.

Feature: write the collector's logs both to the console (container stdout) and
to a rotating file in the shared log directory, so the web UI can tail them
alongside the other services' log files. Verbosity comes from `LOG_LEVEL`;
rotation (size + retained copies) from `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT`.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import Settings

# The collector's own log file inside the shared log directory.
COLLECTOR_LOG_FILE = "collector.log"

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(settings: Settings) -> None:
    """Set up root logging: a console handler plus a rotating file handler.

    The file handler is best-effort -- if the shared directory cannot be
    written (e.g. not mounted), logging falls back to console only.
    """
    formatter = logging.Formatter(_FORMAT)
    root = logging.getLogger()
    root.setLevel(settings.log_level)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        log_dir = Path(settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / COLLECTOR_LOG_FILE,
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # directory not mounted / not writable
        root.warning("File logging disabled (%s): %s", settings.log_dir, exc)
