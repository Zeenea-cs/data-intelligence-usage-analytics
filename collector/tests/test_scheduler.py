"""Tests for the collection cycle's run-record and failure handling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import ApiError
from app.models import Base, CollectionRun
from app.scheduler import execute_collection


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


class _FakeUsers:
    async def export_all_users(self) -> list[dict[str, str]]:
        return [{"Id": "u1", "Email": "u1@x.com", "First name": "A", "Last name": "B", "License type": "Steward", "Permission set id": "p1"}]

    async def list_permission_sets(self) -> list[dict[str, Any]]:
        return [{"id": "p1", "name": "Super Admin", "licenseType": "Steward"}]

    async def iter_scim_users(self) -> AsyncIterator[dict[str, Any]]:
        if False:  # pragma: no cover - empty async generator
            yield {}


class _FakeAudit:
    async def iter_item_events(
        self, *, since: str | None = None, until: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        yield {
            "id": "evt-1",
            "timestamp": "2026-06-15T15:30:10.976Z",
            "eventType": "Item",
            "origin": {"id": "u1", "originType": "User"},
            "itemId": "i1",
            "itemName": "Item One",
            "itemEventType": "CreateItem",
        }


class _BoomAudit:
    async def iter_item_events(
        self, *, since: str | None = None, until: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        raise ApiError("audit down")
        yield {}  # pragma: no cover - makes this a generator


class _FakeCatalog:
    async def fetch_items(
        self, refs: Any, *, batch_size: int = 25
    ) -> dict[str, dict[str, Any]]:
        return {}


def test_execute_collection_success_writes_run_row() -> None:
    asyncio.run(_run_success())


async def _run_success() -> None:
    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    status = await execute_collection(
        factory,
        users_client=_FakeUsers(),
        audit_client=_FakeAudit(),
        catalog_client=_FakeCatalog(),
    )
    assert status == "success"

    async with factory() as session:
        run = (await session.execute(select(CollectionRun))).scalar_one()
    assert run.status == "success"
    assert run.users_collected == 1
    assert run.events_collected == 1
    assert run.items_collected == 0
    assert run.error_message is None
    assert run.started_at is not None and run.finished_at is not None

    await engine.dispose()


def test_execute_collection_partial_on_collector_failure() -> None:
    asyncio.run(_run_partial())


async def _run_partial() -> None:
    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    status = await execute_collection(
        factory,
        users_client=_FakeUsers(),
        audit_client=_BoomAudit(),
        catalog_client=_FakeCatalog(),
    )
    assert status == "partial"

    async with factory() as session:
        run = (await session.execute(select(CollectionRun))).scalar_one()
    assert run.status == "partial"
    assert run.users_collected == 1  # users still committed
    assert run.events_collected == 0
    assert "events" in run.error_message

    await engine.dispose()
