"""Audit API client (REST).

Confirmed contract (see DISCOVERY.md §1):
- Endpoint: POST /public-api/management/audit (POST, not GET).
- Headers: X-API-SECRET, Content-Type: application/json.
- Request body: {"eventType": "Item", "from": <iso>, "to": <iso>,
  "cursorMark": <cursor>}. `eventType`, `from` and `to` are all REQUIRED -- a
  missing bound returns HTTP 500 `error.path.missing` (confirmed live). When the
  caller passes no window the client defaults to a look-back ending now.
- Pagination: cursor-based via `pagination.cursorMark`. Resend the same body
  with `cursorMark` set to the previous response's value. Stop when `data` is
  empty or the returned cursorMark is "". `pageSize` is ignored.
- Response envelope: {"data": [...], "pagination": {"cursorMark": "..."}}.

Field mapping (Item event -> audit_events), confirmed:
    id            -> event_id        (unique upsert key)
    origin.id     -> user_id         (only when origin.originType == "User")
    (lookup)      -> username        (from users by user_id; applied in collector)
    itemEventType -> action          (CreateItem / UpdateItem / DeleteItem)
    itemId        -> item_id
    eventType     -> item_type       (constant "Item")
    itemName      -> item_name
    timestamp     -> occurred_at
    (whole event) -> raw_payload
    (now)         -> collected_at     (set in collector)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.api import ApiError

logger = logging.getLogger(__name__)

AUDIT_PATH = "/public-api/management/audit"
ITEM_EVENT_TYPE = "Item"

# `from`/`to` are REQUIRED by the live API (a missing bound returns HTTP 500
# `error.path.missing`). When the caller gives no window, default to this
# look-back ending now.
_DEFAULT_LOOKBACK_DAYS = 365


def _iso_millis(moment: datetime) -> str:
    """Format a datetime as ISO-8601 with millisecond precision and a Z suffix."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class AuditClient:
    """Client for the REST Audit API. Handles cursor pagination over events."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0) -> None:
        """Store the instance base URL and API key for X-API-SECRET auth."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        """Auth + content headers for every Audit request."""
        return {
            "X-API-SECRET": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def iter_item_events(
        self, *, since: str | None = None, until: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield raw `Item` audit events, following the cursorMark pagination.

        Args:
            since: ISO-8601 window start (`from`). Defaults to the look-back.
            until: ISO-8601 window end (`to`). Defaults to now.

        Raises:
            ApiError: on any HTTP or transport failure (already logged).
        """
        now = datetime.now(timezone.utc)
        if until is None:
            until = _iso_millis(now)
        if since is None:
            since = _iso_millis(now - timedelta(days=_DEFAULT_LOOKBACK_DAYS))
        body: dict[str, Any] = {
            "eventType": ITEM_EVENT_TYPE,
            "from": since,
            "to": until,
        }

        cursor: str | None = None
        async with httpx.AsyncClient(
            base_url=self._base_url, headers=self._headers(), timeout=self._timeout
        ) as client:
            while True:
                payload = dict(body)
                if cursor:
                    payload["cursorMark"] = cursor
                data = await self._post(client, payload)

                events = data.get("data") or []
                if not events:
                    break
                for event in events:
                    yield event

                cursor = (data.get("pagination") or {}).get("cursorMark")
                if not cursor:
                    break

    async def _post(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """POST one page; raise ApiError (after logging) on failure."""
        try:
            response = await client.post(AUDIT_PATH, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Audit API returned HTTP %s: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise ApiError("Audit API request failed") from exc
        except httpx.HTTPError as exc:
            logger.error("Audit API request error: %s", exc)
            raise ApiError("Audit API request failed") from exc
        return response.json()
