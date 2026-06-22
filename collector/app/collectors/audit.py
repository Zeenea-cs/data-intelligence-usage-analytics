"""Audit event collector.

Collects `Item` events from the Audit API and upserts them into the
`audit_events` table (`INSERT ... ON CONFLICT (event_id) DO UPDATE`), so a
duplicate event never produces a second row. Field mapping follows DISCOVERY.md
§1; `username` is backfilled from the `users` table by `origin.id`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.audit import AuditClient
from app.database import upsert_rows
from app.models import AuditEvent, User
from app.timeutils import parse_iso_timestamp

logger = logging.getLogger(__name__)

# Columns overwritten on conflict (everything except the surrogate PK `id` and
# the unique `event_id` conflict key).
_UPDATE_COLUMNS = (
    "user_id",
    "username",
    "action",
    "item_id",
    "item_type",
    "item_name",
    "occurred_at",
    "raw_payload",
    "collected_at",
)

async def _load_usernames(session: AsyncSession) -> dict[str, str | None]:
    """Return an id -> username map of known users for backfill."""
    result = await session.execute(select(User.id, User.username))
    return {row.id: row.username for row in result.all()}


async def collect_audit_events(
    session: AsyncSession,
    client: AuditClient,
    *,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """Collect and upsert `Item` audit events. Return the count processed.

    Args:
        since: optional ISO-8601 window start passed to the client.
        until: optional ISO-8601 window end passed to the client.

    Raises:
        ApiError: propagated from the client on upstream failure (logged there);
            the caller records the run as partial.
    """
    usernames = await _load_usernames(session)
    now = datetime.now(timezone.utc)

    # Keyed by event_id so a duplicate event seen within one run (e.g. overlapping
    # pages) collapses to a single row, last occurrence winning. Cross-run dedup is
    # handled separately by the ON CONFLICT (event_id) DO UPDATE upsert below; a
    # repeated event_id in one multi-row INSERT would otherwise raise "ON CONFLICT
    # DO UPDATE command cannot affect row a second time".
    db_rows: dict[str, dict[str, Any]] = {}
    async for event in client.iter_item_events(since=since, until=until):
        origin = event.get("origin") or {}
        origin_id = origin.get("id") if origin.get("originType") == "User" else None
        # Only set the FK when the user is known, to avoid a constraint failure;
        # the full origin remains available in raw_payload regardless.
        user_id = origin_id if origin_id in usernames else None
        db_rows[event["id"]] = {
            "event_id": event["id"],
            "user_id": user_id,
            "username": usernames.get(origin_id),
            "action": event.get("itemEventType"),
            "item_id": event.get("itemId"),
            "item_type": event.get("eventType"),
            "item_name": event.get("itemName"),
            "occurred_at": parse_iso_timestamp(event.get("timestamp")),
            "raw_payload": event,
            "collected_at": now,
        }

    count = await upsert_rows(
        session,
        AuditEvent.__table__,
        list(db_rows.values()),
        index_elements=["event_id"],
        update_columns=_UPDATE_COLUMNS,
    )
    logger.info("Collected %s audit events", count)
    return count
