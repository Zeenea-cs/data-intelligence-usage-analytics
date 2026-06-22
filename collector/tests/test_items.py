"""Tests for the Catalog item-fetch client and the items collector."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import ApiError
from app.api.catalog import CatalogClient
from app.collectors.items import collect_items
from app.models import AuditEvent, Base, Item

BASE = "https://test.example"


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


def _node(ref: str, name: str) -> dict[str, Any]:
    return {
        "id": ref,
        "key": f"businessconcept/{name}",
        "name": name,
        "type": "businessconcept",
        "completion": 0.5,
        "lastCatalogMetadataUpdate": "2026-02-20T11:29:08.120Z",
        "lifecycleStage": "PRODUCTION",
        "shared": True,
        "catalogCode": "C1",
        "orphan": False,
        "descriptionV2": {
            "content": {"content": "<p>desc</p>", "contentType": "HTML"},
            "summary": f"Summary of {name}",
            "lifecycle": "USER_DEFINED",
        },
        "curators": {
            "edges": [
                {
                    "node": {
                        "id": "owner-1",
                        "name": "David Martin",
                        "key": "dmartin@zeenea.com",
                        "type": "contact",
                    }
                }
            ]
        },
    }


# --------------------------------------------------------------------------- #
# CatalogClient.fetch_items
# --------------------------------------------------------------------------- #


@respx.mock
def test_fetch_items_per_item_tolerates_not_found() -> None:
    """fetch_items returns found nodes and skips ITEM_NOT_FOUND refs (one req each)."""
    found_node = _node("uuid-1", "Customer")

    def handler(request: httpx.Request) -> httpx.Response:
        ref = json.loads(request.content)["variables"]["r"]
        if ref == "uuid-1":
            return httpx.Response(200, json={"data": {"item": found_node}})
        return httpx.Response(
            200,
            json={
                "data": None,
                "errors": [
                    {
                        "message": f"Item '{ref}' has not been found",
                        "extensions": {"code": "ITEM_NOT_FOUND", "value": ref},
                    }
                ],
            },
        )

    route = respx.post(f"{BASE}/api/catalog/graphql").mock(side_effect=handler)
    client = CatalogClient(BASE, "secret-key")
    result = asyncio.run(client.fetch_items(["uuid-1", "uuid-2"]))

    assert set(result.keys()) == {"uuid-1"}
    assert result["uuid-1"]["name"] == "Customer"
    assert route.call_count == 2  # one request per ref


@respx.mock
def test_fetch_items_raises_on_real_graphql_error() -> None:
    """A non-not-found GraphQL error still raises ApiError."""
    respx.post(f"{BASE}/api/catalog/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"data": {}, "errors": [{"message": "boom", "extensions": {"code": "X"}}]},
        )
    )
    client = CatalogClient(BASE, "k")
    with pytest.raises(ApiError):
        asyncio.run(client.fetch_items(["uuid-1"]))


@respx.mock
def test_fetch_items_logs_item_ref_on_error(caplog: pytest.LogCaptureFixture) -> None:
    """An item-related Catalog error names the offending item (key/UUID)."""
    respx.post(f"{BASE}/api/catalog/graphql").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": None,
                "errors": [
                    {
                        "message": "not found",
                        "extensions": {"code": "ITEM_NOT_FOUND", "value": "uuid-XYZ"},
                    }
                ],
            },
        )
    )
    client = CatalogClient(BASE, "secret-key")
    with caplog.at_level(logging.INFO):
        asyncio.run(client.fetch_items(["uuid-XYZ"]))
    assert "uuid-XYZ" in caplog.text  # the item ref is in the log message


# --------------------------------------------------------------------------- #
# Items collector
# --------------------------------------------------------------------------- #


class _FakeCatalog:
    """Returns canned nodes; records which refs were requested."""

    def __init__(self, nodes: dict[str, dict[str, Any]]) -> None:
        self._nodes = nodes
        self.requested: list[list[str]] = []

    async def fetch_items(
        self, refs: Sequence[str], *, batch_size: int = 25
    ) -> dict[str, dict[str, Any]]:
        self.requested.append(list(refs))
        return {r: self._nodes[r] for r in refs if r in self._nodes}


async def _seed_audit(factory: async_sessionmaker[Any], item_ids: list[str]) -> None:
    async with factory() as session:
        for i, iid in enumerate(item_ids):
            session.add(
                AuditEvent(
                    event_id=f"e{i}",
                    item_id=iid,
                    item_name=f"n{i}",
                    occurred_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                )
            )
        await session.commit()


def test_collect_items_maps_owner_and_skips_deleted() -> None:
    asyncio.run(_run_collect_items())


async def _run_collect_items() -> None:
    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # uuid-1 exists in catalog, uuid-2 is deleted (not returned).
    await _seed_audit(factory, ["uuid-1", "uuid-2"])
    catalog = _FakeCatalog({"uuid-1": _node("uuid-1", "Customer")})

    async with factory() as session:
        count = await collect_items(session, catalog)
        await session.commit()
    assert count == 1

    async with factory() as session:
        item = await session.get(Item, "uuid-1")
        total = await session.scalar(select(func.count()).select_from(Item))

    assert total == 1
    assert item.key == "businessconcept/Customer"
    assert item.item_type == "businessconcept"
    assert item.description == "Summary of Customer"
    assert item.description_type == "HTML"
    assert item.owner_id == "owner-1"
    assert item.owner_name == "David Martin"
    assert item.owner_email == "dmartin@zeenea.com"
    assert item.last_updated_at.year == 2026
    assert item.attributes["descriptionLifecycle"] == "USER_DEFINED"

    await engine.dispose()


def test_collect_items_incremental_skips_fresh() -> None:
    asyncio.run(_run_incremental())


async def _run_incremental() -> None:
    engine = await _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _seed_audit(factory, ["uuid-1"])
    catalog = _FakeCatalog({"uuid-1": _node("uuid-1", "Customer")})

    async with factory() as session:
        await collect_items(session, catalog)
        await session.commit()
    # Second run: uuid-1 was just fetched, so it is fresh and not refetched.
    async with factory() as session:
        count = await collect_items(session, catalog)
        await session.commit()

    assert count == 0
    assert catalog.requested[0] == ["uuid-1"]  # fetched on first run
    assert len(catalog.requested) == 1  # not called again on the second run

    await engine.dispose()
