"""Tests for collectors.

Steward detection and upsert idempotency are exercised against an in-memory
async SQLite database. API clients are replaced with fakes returning payloads
shaped like the responses observed during discovery (DISCOVERY.md); no live
Actian instance is required.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.collectors.audit import collect_audit_events
from app.collectors.users import collect_users, is_steward
from app.models import AuditEvent, Base, User
from app.timeutils import parse_iso_timestamp


# --------------------------------------------------------------------------- #
# Test helpers
# --------------------------------------------------------------------------- #


async def _make_engine() -> AsyncEngine:
    """Create an in-memory SQLite engine with the schema applied."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


class FakeUsersClient:
    """Stand-in for UsersClient returning canned export/permission/SCIM data."""

    def __init__(
        self,
        export_rows: list[dict[str, str]],
        permission_sets: list[dict[str, Any]],
        scim_users: list[dict[str, Any]],
    ) -> None:
        self._export_rows = export_rows
        self._permission_sets = permission_sets
        self._scim_users = scim_users

    async def export_all_users(self) -> list[dict[str, str]]:
        return self._export_rows

    async def list_permission_sets(self) -> list[dict[str, Any]]:
        return self._permission_sets

    async def iter_scim_users(self) -> AsyncIterator[dict[str, Any]]:
        for user in self._scim_users:
            yield user


class FakeAuditClient:
    """Stand-in for AuditClient yielding canned Item events."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def iter_item_events(
        self, *, since: str | None = None, until: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        for event in self._events:
            yield event


# --------------------------------------------------------------------------- #
# Steward detection
# --------------------------------------------------------------------------- #


def test_steward_detection_from_license_type() -> None:
    """is_steward follows the confirmed License-type rule (case-insensitive)."""
    assert is_steward("Steward") is True
    assert is_steward("steward") is True
    assert is_steward("  Steward  ") is True
    assert is_steward("Explorer") is False
    assert is_steward("") is False
    assert is_steward(None) is False


# --------------------------------------------------------------------------- #
# Users collector
# --------------------------------------------------------------------------- #

_PERMISSION_SETS = [
    {
        "id": "b5df8dee",
        "name": "Super Admin",
        "description": "all access",
        "builtIn": True,
        "licenseType": "Steward",
        "permissions": [{"permission": "CatalogDesign"}],
    },
    {
        "id": "cec5c187",
        "name": "Explorer",
        "description": "read",
        "builtIn": False,
        "licenseType": "Explorer",
        "permissions": [],
    },
]


def _export_row(uid: str, email: str, ps_id: str, license_type: str) -> dict[str, str]:
    return {
        "Id": uid,
        "Email": email,
        "First name": "Jane",
        "Last name": "Doe",
        "Phone": "",
        "Permission set id": ps_id,
        "Permission set name": "n/a",
        "Permission set built-in": "false",
        "License type": license_type,
        "Creation date": "2023-12-12T10:16:37Z",
        "Last login": "2026-06-18T13:42:28Z",
        "Logins count": "600",
    }


_SCIM_USERS = [
    {
        "id": "u-admin",
        "userName": "admin@x.com",
        "active": True,
        "groups": [{"value": "g1", "display": "Super Admin"}],
    }
]


def test_collect_users_maps_and_upserts() -> None:
    asyncio.run(_run_collect_users_maps_and_upserts())


async def _run_collect_users_maps_and_upserts() -> None:
    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    client = FakeUsersClient(
        export_rows=[
            _export_row("u-admin", "admin@x.com", "b5df8dee", "Steward"),
            _export_row("u-expl", "expl@x.com", "cec5c187", "Explorer"),
        ],
        permission_sets=_PERMISSION_SETS,
        scim_users=_SCIM_USERS,
    )

    async with factory() as session:
        count = await collect_users(session, client)
        await session.commit()
    assert count == 2

    async with factory() as session:
        admin = await session.get(User, "u-admin")
        expl = await session.get(User, "u-expl")

    assert admin is not None and expl is not None
    assert admin.email == "admin@x.com"
    assert admin.display_name == "Jane Doe"
    assert admin.is_steward is True
    assert expl.is_steward is False
    # roles resolved from listPermissionSets by Permission set id.
    assert admin.roles["name"] == "Super Admin"
    assert admin.roles["licenseType"] == "Steward"
    # attributes carry export activity + SCIM enrichment.
    assert admin.attributes["Logins count"] == "600"
    assert admin.attributes["scimActive"] is True
    assert admin.attributes["scimGroups"][0]["display"] == "Super Admin"

    await engine.dispose()


def test_collect_users_idempotent_preserves_first_seen() -> None:
    asyncio.run(_run_collect_users_idempotent())


async def _run_collect_users_idempotent() -> None:
    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    client = FakeUsersClient(
        export_rows=[_export_row("u-admin", "admin@x.com", "b5df8dee", "Steward")],
        permission_sets=_PERMISSION_SETS,
        scim_users=_SCIM_USERS,
    )

    async with factory() as session:
        await collect_users(session, client)
        await session.commit()
    async with factory() as session:
        first_seen = (await session.get(User, "u-admin")).first_seen_at

    # Second run: same user -> update, not insert.
    async with factory() as session:
        await collect_users(session, client)
        await session.commit()

    async with factory() as session:
        total = await session.scalar(select(func.count()).select_from(User))
        again = await session.get(User, "u-admin")

    assert total == 1
    assert again.first_seen_at == first_seen

    await engine.dispose()


# --------------------------------------------------------------------------- #
# Audit collector
# --------------------------------------------------------------------------- #


def _item_event(event_id: str, origin_id: str, action: str = "UpdateItem") -> dict:
    return {
        "id": event_id,
        "timestamp": "2026-06-15T15:30:10.976397254Z",
        "eventType": "Item",
        "origin": {"id": origin_id, "originType": "User"},
        "itemId": "item-1",
        "itemName": "Some Item",
        "itemEventType": action,
    }


def test_collect_audit_upsert_no_duplicates_and_backfills_username() -> None:
    asyncio.run(_run_collect_audit())


async def _run_collect_audit() -> None:
    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Seed a known user so origin.id resolves to a username and FK is satisfied.
    async with factory() as session:
        session.add(User(id="55b1e042", username="dmartin@x.com", is_steward=True))
        await session.commit()

    events = [
        _item_event("evt-1", "55b1e042", action="CreateItem"),
        _item_event("evt-2", "unknown-user"),  # origin not in users table
    ]
    client = FakeAuditClient(events)

    async with factory() as session:
        count = await collect_audit_events(session, client)
        await session.commit()
    assert count == 2

    # Re-collect the same events -> upsert, no new rows.
    async with factory() as session:
        await collect_audit_events(session, client)
        await session.commit()

    async with factory() as session:
        total = await session.scalar(select(func.count()).select_from(AuditEvent))
        e1 = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.event_id == "evt-1")
            )
        ).scalar_one()
        e2 = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.event_id == "evt-2")
            )
        ).scalar_one()

    assert total == 2  # idempotent: still two rows after the second run
    # Mapping checks.
    assert e1.action == "CreateItem"
    assert e1.item_type == "Item"
    assert e1.item_name == "Some Item"
    assert e1.user_id == "55b1e042"
    assert e1.username == "dmartin@x.com"
    assert e1.occurred_at is not None and e1.occurred_at.year == 2026
    # Unknown origin: FK left null, username null, payload still retained.
    assert e2.user_id is None
    assert e2.username is None
    assert e2.raw_payload["origin"]["id"] == "unknown-user"

    await engine.dispose()


def test_parse_timestamp_handles_nanoseconds_and_z() -> None:
    """Nanosecond precision and trailing Z are normalised to a tz-aware datetime."""
    parsed = parse_iso_timestamp("2026-06-15T15:30:10.976397254Z")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.month == 6 and parsed.day == 15
    assert parse_iso_timestamp(None) is None
    assert parse_iso_timestamp("not-a-date") is None
