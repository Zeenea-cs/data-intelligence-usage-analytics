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
from app.models import AuditEvent, Base, User, UserSnapshot
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
        "id": "66666666",
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
            _export_row("u-admin", "admin@x.com", "66666666", "Steward"),
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
        export_rows=[_export_row("u-admin", "admin@x.com", "66666666", "Steward")],
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


def test_collect_users_appends_snapshot_each_run() -> None:
    asyncio.run(_run_collect_users_snapshots())


async def _run_collect_users_snapshots() -> None:
    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    client = FakeUsersClient(
        export_rows=[
            _export_row("u-admin", "admin@x.com", "66666666", "Steward"),
            _export_row("u-expl", "expl@x.com", "cec5c187", "Explorer"),
        ],
        permission_sets=_PERMISSION_SETS,
        scim_users=_SCIM_USERS,
    )

    # Two runs: users table stays at 2 rows (upsert); snapshots accumulate.
    async with factory() as session:
        await collect_users(session, client)
        await session.commit()
    async with factory() as session:
        await collect_users(session, client)
        await session.commit()

    async with factory() as session:
        users = await session.scalar(select(func.count()).select_from(User))
        snaps = await session.scalar(select(func.count()).select_from(UserSnapshot))
        stewards = await session.scalar(
            select(func.count())
            .select_from(UserSnapshot)
            .where(UserSnapshot.is_steward)
        )
        admin_snap = (
            await session.execute(
                select(UserSnapshot).where(UserSnapshot.user_id == "u-admin").limit(1)
            )
        ).scalar_one()

    assert users == 2  # upserted live rows
    assert snaps == 4  # 2 users x 2 runs, append-only
    assert stewards == 2  # one steward per run
    assert admin_snap.license_type == "Steward"
    assert admin_snap.snapshot_at is not None

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
        session.add(User(id="22222222", username="alex.curator@example.com", is_steward=True))
        await session.commit()

    api_key_event = _item_event("evt-3", "key-1")
    api_key_event["origin"]["originType"] = "ApiKey"
    events = [
        _item_event("evt-1", "22222222", action="CreateItem"),
        _item_event("evt-2", "unknown-user"),  # origin not in users table
        api_key_event,
    ]
    client = FakeAuditClient(events)

    async with factory() as session:
        count = await collect_audit_events(session, client)
        await session.commit()
    assert count == 3

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

    assert total == 3  # idempotent: still three rows after the second run
    # Mapping checks.
    assert e1.action == "CreateItem"
    assert e1.item_type == "Item"
    assert e1.item_name == "Some Item"
    assert e1.user_id == "22222222"
    assert e1.username == "alex.curator@example.com"
    assert e1.occurred_at is not None and e1.occurred_at.year == 2026
    # Unknown origin: FK left null, username null, payload still retained.
    assert e2.user_id is None
    assert e2.username is None
    assert e2.raw_payload["origin"]["id"] == "unknown-user"
    # ...but the edit is still attributed. A user who left before the first
    # collection used to vanish from every activity statistic, because the cards
    # filtered on user_id; they now count on actor_id.
    assert e1.actor_id == "22222222"
    assert e2.actor_id == "unknown-user"
    async with factory() as session:
        e3 = (
            await session.execute(select(AuditEvent).where(AuditEvent.event_id == "evt-3"))
        ).scalar_one()
    assert e3.actor_id is None  # an API key is not a contributor

    # A subsequent run with a CHANGED payload for an existing event_id updates the
    # row in place -- no new row is added.
    changed = _item_event("evt-1", "22222222", action="DeleteItem")
    changed["itemName"] = "Renamed Item"
    async with factory() as session:
        await collect_audit_events(session, FakeAuditClient([changed]))
        await session.commit()

    async with factory() as session:
        total_after = await session.scalar(select(func.count()).select_from(AuditEvent))
        e1_after = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.event_id == "evt-1")
            )
        ).scalar_one()
    assert total_after == 3  # still three rows: updated, not inserted
    assert e1_after.action == "DeleteItem"
    assert e1_after.item_name == "Renamed Item"

    await engine.dispose()


def test_collect_audit_dedupes_duplicate_event_id_within_run() -> None:
    """The same event_id seen twice in one run collapses to a single row."""
    asyncio.run(_run_collect_audit_intra_run_dedup())


async def _run_collect_audit_intra_run_dedup() -> None:
    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Same event_id twice (e.g. overlapping pages); second occurrence differs.
    first = _item_event("dup-1", "unknown", action="CreateItem")
    second = _item_event("dup-1", "unknown", action="UpdateItem")
    client = FakeAuditClient([first, second])

    async with factory() as session:
        count = await collect_audit_events(session, client)
        await session.commit()
    assert count == 1  # collapsed before insert

    async with factory() as session:
        total = await session.scalar(select(func.count()).select_from(AuditEvent))
        row = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.event_id == "dup-1")
            )
        ).scalar_one()
    assert total == 1
    assert row.action == "UpdateItem"  # last occurrence wins

    await engine.dispose()


def test_upsert_rows_chunks_large_batches() -> None:
    """A batch whose (rows * columns) exceeds the bind-param limit is chunked.

    audit_events has 10 columns; 5000 rows -> 50000 params, over SQLite's 32766
    (and a stand-in for PostgreSQL's 65535). Without chunking this raises; the
    upsert must succeed and persist every row.
    """
    asyncio.run(_run_upsert_rows_chunks_large_batches())


async def _run_upsert_rows_chunks_large_batches() -> None:
    from datetime import datetime, timezone

    from app.database import upsert_rows

    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    rows = [
        {
            "event_id": f"evt-{i}",
            "user_id": None,
            "username": None,
            "action": "CreateItem",
            "item_id": f"item-{i}",
            "item_type": "Item",
            "item_name": "Some Item",
            "occurred_at": now,
            "raw_payload": {"id": f"evt-{i}"},
            "collected_at": now,
        }
        for i in range(5000)
    ]

    async with factory() as session:
        count = await upsert_rows(
            session,
            AuditEvent.__table__,
            rows,
            index_elements=["event_id"],
            update_columns=("action", "item_name"),
        )
        await session.commit()
    assert count == 5000

    async with factory() as session:
        total = await session.scalar(select(func.count()).select_from(AuditEvent))
    assert total == 5000

    await engine.dispose()


def test_reinit_data_clears_data_keeps_runs() -> None:
    """reinit_data empties users/items/audit_events but keeps runs + snapshots."""
    asyncio.run(_run_reinit_data())


async def _run_reinit_data() -> None:
    from datetime import datetime, timezone

    from app.database import reinit_data
    from app.models import CollectionRun, Item

    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)

    async with factory() as session:
        session.add(User(id="u1", username="u1@x.com", is_steward=False))
        session.add(Item(id="i1", item_type="dataset", name="I1"))
        session.add(
            AuditEvent(event_id="e1", action="CreateItem", occurred_at=now, collected_at=now)
        )
        session.add(CollectionRun(started_at=now, finished_at=now, status="success"))
        session.add(UserSnapshot(snapshot_at=now, user_id="u1", is_steward=False))
        await session.commit()

    async with factory() as session:
        await reinit_data(session)
        await session.commit()

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0
        assert await session.scalar(select(func.count()).select_from(Item)) == 0
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 0
        # Run history and snapshot history are preserved.
        assert await session.scalar(select(func.count()).select_from(CollectionRun)) == 1
        assert await session.scalar(select(func.count()).select_from(UserSnapshot)) == 1

    await engine.dispose()


def test_force_reload_replaces_stale_data() -> None:
    """force_reload wipes pre-existing rows and reloads via the collectors."""
    asyncio.run(_run_force_reload())


async def _run_force_reload() -> None:
    from app import scheduler
    from app.models import CollectionRun

    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = parse_iso_timestamp("2026-06-20T00:00:00.000Z")

    # Seed a stale audit event that the reload must remove (not in fresh data).
    async with factory() as session:
        session.add(AuditEvent(event_id="stale", action="X", occurred_at=now, collected_at=now))
        await session.commit()

    # Fresh data the reload pulls in; only the audit collector runs for real.
    fresh = FakeAuditClient([_item_event("fresh-1", "unknown")])

    class _Clients:
        users = object()
        audit = fresh
        catalog = object()

    async def _zero_users(session: Any, client: Any) -> int:
        return 0

    async def _zero_items(session: Any, client: Any) -> int:
        return 0

    # Patch the scheduler's collaborators so no network or other collector runs.
    originals = (scheduler.build_clients, scheduler.collect_users, scheduler.collect_items)
    scheduler.build_clients = lambda settings: _Clients()  # type: ignore[assignment]
    scheduler.collect_users = _zero_users  # type: ignore[assignment]
    scheduler.collect_items = _zero_items  # type: ignore[assignment]
    try:
        status = await scheduler.force_reload(object(), factory, days=10)
    finally:
        scheduler.build_clients, scheduler.collect_users, scheduler.collect_items = originals
    assert status == "success"

    async with factory() as session:
        ids = (await session.execute(select(AuditEvent.event_id))).scalars().all()
        assert "stale" not in ids  # wiped
        assert "fresh-1" in ids  # reloaded
        # Run history row recorded by the reload.
        assert await session.scalar(select(func.count()).select_from(CollectionRun)) == 1

    await engine.dispose()


def test_incremental_since_uses_last_successful_run() -> None:
    """_incremental_since returns the latest successful run start, else None."""
    asyncio.run(_run_incremental_since())


async def _run_incremental_since() -> None:
    from datetime import datetime, timezone

    from app.models import CollectionRun
    from app.scheduler import _incremental_since

    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # No runs yet -> None (first run falls back to the configured look-back).
    assert await _incremental_since(factory) is None

    older = datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 6, 22, 9, 30, tzinfo=timezone.utc)
    future_fail = datetime(2026, 6, 23, 9, 30, tzinfo=timezone.utc)
    async with factory() as session:
        session.add(CollectionRun(started_at=older, finished_at=older, status="success"))
        session.add(CollectionRun(started_at=newer, finished_at=newer, status="success"))
        # A later FAILED run must be ignored (would otherwise skip events).
        session.add(
            CollectionRun(started_at=future_fail, finished_at=future_fail, status="failed")
        )
        await session.commit()

    since = await _incremental_since(factory)
    assert since is not None
    # Returns the most recent *successful* run (2026-06-22), not the older success
    # nor the later FAILED run (2026-06-23, which would skip events).
    assert since.startswith("2026-06-22")

    await engine.dispose()


def test_parse_timestamp_handles_nanoseconds_and_z() -> None:
    """Nanosecond precision and trailing Z are normalised to a tz-aware datetime."""
    parsed = parse_iso_timestamp("2026-06-15T15:30:10.976397254Z")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.month == 6 and parsed.day == 15
    assert parse_iso_timestamp(None) is None
    assert parse_iso_timestamp("not-a-date") is None
