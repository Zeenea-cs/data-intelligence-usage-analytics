"""Smoke tests for the trigger web UI (FastAPI)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.config import Settings
from app.models import Base, CollectionRun
from app.webui import create_app

_SETTINGS = Settings(
    actian_instance_url="https://x",
    actian_api_key="k",
    collect_cron="0 0 * * *",
    postgres_host="db",
    postgres_port=5432,
    postgres_db="d",
    postgres_user="u",
    postgres_password="p",
    metabase_db_password="m",
    log_level="INFO",
)


def _build() -> tuple[Any, Any]:
    async def setup() -> Any:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                CollectionRun(
                    started_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
                    finished_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
                    status="success",
                    users_collected=42,
                    events_collected=5678,
                    items_collected=12,
                )
            )
            await session.commit()
        return engine, factory

    return asyncio.run(setup())


def test_index_and_runs_endpoints() -> None:
    _engine, factory = _build()
    app = create_app(_SETTINGS, factory, asyncio.Lock())
    with TestClient(app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "Run collection now" in index.text

        runs = client.get("/api/runs")
        assert runs.status_code == 200
        data = runs.json()
        assert len(data) == 1
        assert data[0]["status"] == "success"
        assert data[0]["events_collected"] == 5678


def test_trigger_returns_202(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    async def _fake_run(settings: Any, factory: Any, lock: Any) -> str:
        calls.append(True)
        return "success"

    monkeypatch.setattr("app.webui.run_collection", _fake_run)
    _engine, factory = _build()
    app = create_app(_SETTINGS, factory, asyncio.Lock())
    with TestClient(app) as client:
        resp = client.post("/api/collect")
        assert resp.status_code == 202
        assert resp.json()["detail"] == "Collection started"
