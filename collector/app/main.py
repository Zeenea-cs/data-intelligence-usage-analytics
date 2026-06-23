"""Collector entrypoint.

Startup sequence:
1. load + validate settings;
2. run Alembic migrations to head (creates/updates the schema);
3. start the AsyncIOScheduler (cron from COLLECT_CRON) and run one immediate
   collection so the database is not empty after deployment;
4. serve the FastAPI trigger web UI, which blocks until the process stops.

The scheduler and the web server share one asyncio event loop and one lock, so
scheduled and manually triggered runs never overlap.
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from app.config import Settings, load_settings
from app.database import create_db_engine, create_session_factory
from app.logsetup import configure_logging
from app.migrate import run_migrations
from app.scheduler import build_scheduler, run_collection
from app.webui import create_app

logger = logging.getLogger(__name__)

INITIAL_JOB_ID = "initial-collection"


async def _serve(settings: Settings) -> None:
    """Start the scheduler + immediate run and serve the UI (schema already migrated)."""
    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    # One shared lock keeps scheduled and web-triggered runs from overlapping.
    lock = asyncio.Lock()
    scheduler = build_scheduler(settings, session_factory, lock)

    # Immediate one-off run on first start (date trigger = now).
    scheduler.add_job(
        run_collection,
        args=[settings, session_factory, lock],
        id=INITIAL_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started (cron=%s)", settings.collect_cron)

    # Serve the web UI in the same event loop; this call blocks until shutdown.
    app = create_app(settings, session_factory, lock)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.webui_host,
            port=settings.webui_port,
            log_level=settings.log_level.lower(),
        )
    )
    logger.info("Web UI on http://%s:%s", settings.webui_host, settings.webui_port)
    try:
        await server.serve()
    finally:
        scheduler.shutdown(wait=False)
        await engine.dispose()


def main() -> None:
    """Load config, configure logging, migrate the database, and run the service."""
    settings = load_settings()
    configure_logging(settings)
    run_migrations(settings.database_url)
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
