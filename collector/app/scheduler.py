"""APScheduler setup and the collection cycle.

Reads COLLECT_CRON and schedules the collection job on an AsyncIOScheduler (the
collectors are async). On each run the collectors execute in order -- users,
then audit events, then items (stub) -- each in its own transaction. A
`collection_runs` row is always recorded; a failing collector marks the run
`partial` (or `failed` if all fail) and never crashes the scheduler.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.audit import AuditClient
from app.api.catalog import CatalogClient
from app.api.users import UsersClient
from app.clients import build_clients
from app.collectors.audit import collect_audit_events
from app.collectors.items import collect_items
from app.collectors.users import collect_users
from app.config import Settings
from app.database import reinit_data, session_scope
from app.models import CollectionRun
from app.timeutils import iso_millis

logger = logging.getLogger(__name__)

JOB_ID = "scheduled-collection"


async def execute_collection(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    users_client: UsersClient,
    audit_client: AuditClient,
    catalog_client: CatalogClient,
    since: str | None = None,
    until: str | None = None,
) -> str:
    """Run the full collection cycle and record a `collection_runs` row.

    Collectors run in order, each in its own transaction so one failure does not
    roll back the others. Returns the run status (``success`` / ``partial`` /
    ``failed``).

    Args:
        since: optional ISO-8601 window start for audit events; when omitted the
            audit client uses its configured look-back (AUDIT_INITIAL_DAYS).
        until: optional ISO-8601 window end for audit events.
    """
    started_at = datetime.now(timezone.utc)
    counts = {"users": 0, "events": 0, "items": 0}
    errors: list[str] = []

    steps = (
        ("users", lambda s: collect_users(s, users_client)),
        ("events", lambda s: collect_audit_events(s, audit_client, since=since, until=until)),
        ("items", lambda s: collect_items(s, catalog_client)),
    )
    for name, run_step in steps:
        try:
            async with session_scope(session_factory) as session:
                counts[name] = await run_step(session)
        except Exception as exc:  # resilience: log, record, continue
            logger.exception("Collector %r failed", name)
            errors.append(f"{name}: {exc}")

    finished_at = datetime.now(timezone.utc)
    if not errors:
        status = "success"
    elif len(errors) == len(steps):
        status = "failed"
    else:
        status = "partial"

    async with session_scope(session_factory) as session:
        session.add(
            CollectionRun(
                started_at=started_at,
                finished_at=finished_at,
                status=status,
                users_collected=counts["users"],
                events_collected=counts["events"],
                items_collected=counts["items"],
                error_message="; ".join(errors) or None,
            )
        )

    logger.info(
        "Collection run %s: users=%s events=%s items=%s",
        status,
        counts["users"],
        counts["events"],
        counts["items"],
    )
    return status


async def _incremental_since(
    session_factory: async_sessionmaker[AsyncSession],
) -> str | None:
    """Return the audit `since` bound for an incremental run, or None.

    Uses the start time of the most recent **successful** collection run, so a
    regular run only fetches audit events since the last good cycle (the upsert
    dedupes the small overlap). Returns None when no successful run exists yet
    (e.g. first start), letting the audit client fall back to its configured
    AUDIT_INITIAL_DAYS look-back for the initial backfill.
    """
    async with session_factory() as session:
        last_started = await session.scalar(
            select(func.max(CollectionRun.started_at)).where(
                CollectionRun.status == "success"
            )
        )
    return iso_millis(last_started) if last_started else None


async def run_collection(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    lock: asyncio.Lock | None = None,
) -> str:
    """Build the API clients from config and run one incremental collection cycle.

    Audit events are fetched since the last successful run (full AUDIT_INITIAL_DAYS
    look-back on the very first run); users and items are always full snapshots.
    When ``lock`` is provided it is held for the whole cycle, so a manual
    (web-triggered) run and the scheduled run never overlap. Returns the run
    status.
    """
    clients = build_clients(settings)
    since = await _incremental_since(session_factory)
    if since:
        logger.info("Incremental collection: audit events since %s", since)
    else:
        logger.info("No prior successful run; using AUDIT_INITIAL_DAYS look-back")

    async def _go() -> str:
        return await execute_collection(
            session_factory,
            users_client=clients.users,
            audit_client=clients.audit,
            catalog_client=clients.catalog,
            since=since,
        )

    # The optional lock serialises manual (web) and scheduled runs.
    if lock is None:
        return await _go()
    async with lock:
        return await _go()


async def force_reload(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    days: int,
    lock: asyncio.Lock | None = None,
) -> str:
    """Wipe collected data and reload the last ``days`` of history.

    Clears ``users``, ``items`` and ``audit_events`` (``collection_runs`` history
    is preserved), then runs a full collection with the audit window bounded to
    the last ``days`` days. Users and items are full snapshots, so they are
    replaced regardless of the window. Holds ``lock`` (when given) for the whole
    operation so it never overlaps a scheduled or manual run. Returns the run
    status.
    """
    clients = build_clients(settings)
    since = iso_millis(datetime.now(timezone.utc) - timedelta(days=days))

    async def _go() -> str:
        async with session_scope(session_factory) as session:
            await reinit_data(session)
        logger.info("Force reload: data cleared, reloading last %s day(s)", days)
        return await execute_collection(
            session_factory,
            users_client=clients.users,
            audit_client=clients.audit,
            catalog_client=clients.catalog,
            since=since,
        )

    if lock is None:
        return await _go()
    async with lock:
        return await _go()


def build_scheduler(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    lock: asyncio.Lock | None = None,
) -> AsyncIOScheduler:
    """Build an AsyncIOScheduler with the collection job registered from COLLECT_CRON."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_collection,
        CronTrigger.from_crontab(settings.collect_cron),
        args=[settings, session_factory, lock],
        id=JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
