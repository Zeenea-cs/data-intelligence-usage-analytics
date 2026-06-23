"""User collector.

Collects users via the User Management bulk export and SCIM, then upserts them
into the `users` table (`INSERT ... ON CONFLICT (id) DO UPDATE`). Field mapping,
the steward rule, and the permission-set resolution all follow DISCOVERY.md.

Per run (DISCOVERY.md consolidated plan):
1. export_all_users()  -> CSV rows (enumeration + License type).
2. list_permission_sets() -> id -> permission set lookup.
3. iter_scim_users()   -> id -> {groups, active}.
4. Resolve each user's Permission set id; upsert.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.users import UsersClient
from app.database import upsert_rows
from app.models import User

logger = logging.getLogger(__name__)

# Columns overwritten on conflict. `first_seen_at` is intentionally excluded so
# it is preserved across re-collection.
_UPDATE_COLUMNS = (
    "username",
    "email",
    "display_name",
    "is_steward",
    "roles",
    "attributes",
    "last_updated_at",
)

# Export CSV columns folded into `attributes` (activity + documentation scopes).
_ATTRIBUTE_CSV_COLUMNS = (
    "Phone",
    "Custom item documentation permission",
    "Custom item documentation scope",
    "Item documentation permission",
    "Item documentation scope",
    "Glossary documentation permission",
    "Glossary documentation scope",
    "Catalog design permission",
    "Ops administration permission",
    "Users and permissions administration permission",
    "Analytics dashboard permission",
    "License type",
    "Creation date",
    "Last login",
    "Logins count",
)


def is_steward(license_type: str | None) -> bool:
    """Return True if the export's `License type` marks a steward.

    Confirmed rule (DISCOVERY.md §3): ``is_steward = License type == "Steward"``.
    Compared case-insensitively against the LicenseType enum value.
    """
    return (license_type or "").strip().lower() == "steward"


def _display_name(row: dict[str, str]) -> str:
    """Join first and last name from an export row."""
    return f"{row.get('First name', '')} {row.get('Last name', '')}".strip()


def _resolve_roles(
    row: dict[str, str], permission_sets: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Resolve the export's `Permission set id` to the full permission set.

    Falls back to the permission-set columns carried in the CSV if the id is not
    found in the listPermissionSets catalogue.
    """
    ps_id = row.get("Permission set id") or ""
    resolved = permission_sets.get(ps_id)
    if resolved is not None:
        return resolved
    return {
        "id": ps_id or None,
        "name": row.get("Permission set name"),
        "description": row.get("Permission set description"),
        "builtIn": row.get("Permission set built-in"),
    }


def _build_attributes(
    row: dict[str, str], scim_user: dict[str, Any]
) -> dict[str, Any]:
    """Assemble the `attributes` JSON from export activity + SCIM enrichment."""
    attributes: dict[str, Any] = {
        key: row.get(key) for key in _ATTRIBUTE_CSV_COLUMNS if row.get(key) is not None
    }
    attributes["scimGroups"] = scim_user.get("groups")
    attributes["scimActive"] = scim_user.get("active")
    return attributes


async def collect_users(session: AsyncSession, client: UsersClient) -> int:
    """Collect and upsert users. Return the count of users processed.

    Raises:
        ApiError: propagated from the client on upstream failure (logged there);
            the caller records the run as partial.
    """
    export_rows = await client.export_all_users()
    # Key by id, skipping any set without one so a malformed entry cannot crash
    # the whole collection (the row falls back to its CSV permission columns).
    permission_sets = {
        ps["id"]: ps for ps in await client.list_permission_sets() if ps.get("id")
    }

    scim_by_id: dict[str, dict[str, Any]] = {}
    async for resource in client.iter_scim_users():
        scim_by_id[resource["id"]] = resource

    now = datetime.now(timezone.utc)
    db_rows: list[dict[str, Any]] = []
    for row in export_rows:
        user_id = row.get("Id")
        if not user_id:
            logger.warning("Skipping export row with no Id: %s", row)
            continue
        email = row.get("Email")
        db_rows.append(
            {
                "id": user_id,
                "username": email,
                "email": email,
                "display_name": _display_name(row),
                "is_steward": is_steward(row.get("License type")),
                "roles": _resolve_roles(row, permission_sets),
                "attributes": _build_attributes(row, scim_by_id.get(user_id, {})),
                "first_seen_at": now,
                "last_updated_at": now,
            }
        )

    count = await upsert_rows(
        session,
        User.__table__,
        db_rows,
        index_elements=["id"],
        update_columns=_UPDATE_COLUMNS,
    )
    logger.info("Collected %s users", count)
    return count
