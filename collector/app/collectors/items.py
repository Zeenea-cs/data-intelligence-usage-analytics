"""Item collector.

Enriches the `items` table from the Catalog API for every item ever seen in the
audit trail. Strategy (confirmed): incremental + refresh-stale -- fetch items not
yet stored, plus re-fetch items whose `last_updated_at` is older than the
staleness window. Deleted/unknown items are skipped (ITEM_NOT_FOUND tolerated).

Field mapping follows DISCOVERY.md §4. Owner is the first "curators" connection
node (a contact: its `key` is the email).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog import CatalogClient
from app.database import upsert_rows
from app.models import AuditEvent, Item
from app.timeutils import parse_iso_timestamp

logger = logging.getLogger(__name__)

# Re-fetch a stored item if its data is older than this many days.
REFRESH_STALE_DAYS = 7

_UPDATE_COLUMNS = (
    "key",
    "item_type",
    "name",
    "description",
    "description_type",
    "owner_id",
    "owner_name",
    "owner_email",
    "attributes",
    "last_updated_at",
)


async def _audit_item_ids(session: AsyncSession) -> list[str]:
    """Return every distinct, non-null item_id seen in the audit trail."""
    result = await session.execute(
        select(AuditEvent.item_id).where(AuditEvent.item_id.is_not(None)).distinct()
    )
    return [row[0] for row in result.all()]


async def _stored_freshness(session: AsyncSession) -> dict[str, datetime | None]:
    """Return id -> our last fetch time (attributes.fetchedAt) for stored items.

    Freshness is tracked separately from ``last_updated_at`` (which holds the
    item's catalog update time, a user-facing field) so re-fetch decisions are
    based on when *we* last fetched, not when the catalog changed.

    Only the ``fetchedAt`` path is extracted in SQL (``->>`` on PostgreSQL,
    ``json_extract`` on SQLite) rather than loading every row's full attributes
    blob into Python.
    """
    fetched_at = Item.attributes["fetchedAt"].as_string()
    result = await session.execute(select(Item.id, fetched_at))
    return {item_id: parse_iso_timestamp(fetched) for item_id, fetched in result.all()}


def _select_refs(
    audit_ids: list[str],
    stored: dict[str, datetime | None],
    stale_before: datetime,
) -> list[str]:
    """Pick refs to fetch: new items + stored items older than stale_before."""
    refs: list[str] = []
    for item_id in audit_ids:
        if item_id not in stored:
            refs.append(item_id)
            continue
        last = stored[item_id]
        if last is None or last < stale_before:
            refs.append(item_id)
    return refs


def _owner(node: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Resolve (owner_id, owner_name, owner_email) from the curators connection."""
    edges = (node.get("curators") or {}).get("edges") or []
    if not edges:
        return None, None, None
    owner = edges[0].get("node") or {}
    email = owner.get("key") if owner.get("type") == "contact" else None
    return owner.get("id"), owner.get("name"), email


def _to_row(node: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Map a Catalog item node to an `items` row."""
    desc = node.get("descriptionV2") or {}
    content = desc.get("content") or {}
    owner_id, owner_name, owner_email = _owner(node)
    return {
        "id": node["id"],
        "key": node.get("key"),
        "item_type": node.get("type"),
        "name": node.get("name"),
        "description": desc.get("summary") or content.get("content"),
        "description_type": content.get("contentType"),
        "owner_id": owner_id,
        "owner_name": owner_name,
        "owner_email": owner_email,
        "attributes": {
            "completion": node.get("completion"),
            "lifecycleStage": node.get("lifecycleStage"),
            "shared": node.get("shared"),
            "catalogCode": node.get("catalogCode"),
            "orphan": node.get("orphan"),
            "descriptionLifecycle": desc.get("lifecycle"),
            # When we fetched this item (drives incremental re-fetch decisions).
            "fetchedAt": now.isoformat(),
        },
        # The item's own last-update time in the catalog (user-facing).
        "last_updated_at": parse_iso_timestamp(node.get("lastCatalogMetadataUpdate"))
        or now,
        "first_seen_at": now,
    }


async def collect_items(session: AsyncSession, client: CatalogClient) -> int:
    """Enrich `items` from the Catalog for audit-trail items. Return count upserted.

    Raises:
        ApiError: propagated from the client on upstream failure (logged there);
            the caller records the run as partial.
    """
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(days=REFRESH_STALE_DAYS)

    audit_ids = await _audit_item_ids(session)
    stored = await _stored_freshness(session)
    refs = _select_refs(audit_ids, stored, stale_before)
    logger.info(
        "Items: %s distinct in audit, %s stored, %s to fetch",
        len(audit_ids),
        len(stored),
        len(refs),
    )
    if not refs:
        return 0

    nodes = await client.fetch_items(refs)
    rows = [_to_row(node, now) for node in nodes.values()]

    count = await upsert_rows(
        session,
        Item.__table__,
        rows,
        index_elements=["id"],
        update_columns=_UPDATE_COLUMNS,
    )
    missing = len(refs) - len(rows)
    logger.info("Collected %s items (%s refs not found / deleted)", count, missing)
    return count
