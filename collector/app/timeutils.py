"""Shared time helpers for parsing and formatting ISO-8601 timestamps."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def iso_millis(moment: datetime) -> str:
    """Format a datetime as ISO-8601 UTC with millisecond precision + `Z`.

    This is the shape the Audit API expects for its `from`/`to` bounds.
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

# Trim sub-second precision to microseconds (Zeenea emits up to nanoseconds,
# which datetime.fromisoformat cannot parse).
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def parse_iso_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (trailing Z / nanoseconds) to a datetime.

    Returns None for empty or unparseable input.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _FRACTION_RE.sub(r"\1", text)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        logger.warning("Unparseable timestamp: %r", value)
        return None
