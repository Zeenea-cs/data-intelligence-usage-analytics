"""Audit API client (REST).

Feature: read the catalog audit trail. Confirmed contract (DISCOVERY.md §1):

* `POST /public-api/management/audit` (POST, not GET); headers X-API-SECRET +
  JSON content.
* Body requires `eventType` (we collect "Item") and a `from`/`to` window -- a
  missing bound returns HTTP 500. When the caller gives no window we default to
  a look-back ending now.
* Pagination is cursor-based: echo back `pagination.cursorMark`; stop when the
  page is empty or the returned cursor is "". (`pageSize` is ignored.)

Field mapping (Item event -> audit_events) is applied in the collector; see
collectors/audit.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

from app.api._http import make_client, secret_headers, send_json
from app.config import DEFAULT_AUDIT_INITIAL_DAYS
from app.timeutils import iso_millis

AUDIT_PATH = "/public-api/management/audit"
ITEM_EVENT_TYPE = "Item"

# `from`/`to` are mandatory; default to the configured look-back ending now when
# unspecified (shares the single source of truth in app.config).
DEFAULT_LOOKBACK_DAYS = DEFAULT_AUDIT_INITIAL_DAYS


class AuditClient:
    """Reads `Item` audit events, following the API's cursor pagination."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._lookback_days = lookback_days

    async def iter_item_events(
        self, *, since: str | None = None, until: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every `Item` audit event in the window, one page at a time.

        Args:
            since: ISO-8601 window start (`from`); defaults to the look-back.
            until: ISO-8601 window end (`to`); defaults to now.

        Raises:
            ApiError: on any upstream failure (already logged).
        """
        now = datetime.now(timezone.utc)
        body: dict[str, Any] = {
            "eventType": ITEM_EVENT_TYPE,
            "from": since or iso_millis(now - timedelta(days=self._lookback_days)),
            "to": until or iso_millis(now),
        }

        headers = secret_headers(self._api_key, json_body=True)
        cursor: str | None = None
        async with make_client(self._base_url, headers, self._timeout) as client:
            while True:
                # Each page resends the same filter plus the previous cursor.
                page_body = {**body, "cursorMark": cursor} if cursor else body
                data = await send_json(
                    client, "POST", AUDIT_PATH, label="Audit API", json=page_body
                )

                events = data.get("data") or []
                if not events:
                    break
                for event in events:
                    yield event

                # Advance; an empty cursor means there are no more pages.
                cursor = (data.get("pagination") or {}).get("cursorMark")
                if not cursor:
                    break
