"""Logging configuration for the collector.

Feature: write the collector's logs both to the console (container stdout) and
to a rotating file in the shared log directory, so the web UI can tail them
alongside the other services' log files. Verbosity comes from `LOG_LEVEL`;
rotation (size + retained copies) from `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT`.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import Settings

# The collector's own log file inside the shared log directory.
COLLECTOR_LOG_FILE = "collector.log"

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# httpx logs every outbound request at INFO, including the presigned S3 URL
# used to download the user export CSV -- its query string carries
# X-Amz-Security-Token and other credential-bearing params, so it must never
# reach console/file output verbatim.
_AMAZONAWS_URL_RE = re.compile(r"(https?://[^\s\"'?]*amazonaws\.com[^\s\"'?]*)\?[^\s\"']*")


class _SensitiveUrlFilter(logging.Filter):
    """Strip the query string off any amazonaws.com URL in a log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.msg = record.getMessage()
            record.args = ()
        record.msg = _AMAZONAWS_URL_RE.sub(r"\1?<redacted>", str(record.msg))
        return True


def configure_logging(settings: Settings) -> None:
    """Set up root logging: a console handler plus a rotating file handler.

    The file handler is best-effort -- if the shared directory cannot be
    written (e.g. not mounted), logging falls back to console only.
    """
    formatter = logging.Formatter(_FORMAT)
    url_filter = _SensitiveUrlFilter()
    root = logging.getLogger()
    root.setLevel(settings.log_level)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(url_filter)
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
        file_handler.addFilter(url_filter)
        root.addHandler(file_handler)
    except OSError as exc:  # directory not mounted / not writable
        root.warning("File logging disabled (%s): %s", settings.log_dir, exc)
