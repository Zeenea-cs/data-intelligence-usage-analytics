"""Collector entrypoint.

On startup: load settings, run Alembic migrations to head, then start the
AsyncIOScheduler. An immediate one-off collection runs on first start so the
database is not empty after deployment; the cron job then runs on COLLECT_CRON.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from app.config import Settings, load_settings
from app.database import create_db_engine, create_session_factory
from app.scheduler import build_scheduler, run_collection
from app.webui import create_app

logger = logging.getLogger(__name__)

# alembic.ini lives at the collector root (one level above this app package).
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

INITIAL_JOB_ID = "initial-collection"


def run_migrations(settings: Settings) -> None:
    """Run Alembic migrations to bring the database to the latest revision."""
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    logger.info("Running Alembic migrations to head")
    command.upgrade(config, "head")


async def _serve(settings: Settings) -> None:
    """Start the scheduler + web UI, trigger an immediate run, and block."""
    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    # Shared lock so a web-triggered run and the cron run never overlap.
    lock = asyncio.Lock()
    scheduler = build_scheduler(settings, session_factory, lock)

    # One immediate collection on first start (runs once, asap).
    scheduler.add_job(
        run_collection,
        args=[settings, session_factory, lock],
        id=INITIAL_JOB_ID,
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started (cron=%s)", settings.collect_cron)

    host = os.environ.get("WEBUI_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBUI_PORT", "8000"))
    app = create_app(settings, session_factory, lock)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level=settings.log_level.lower())
    )
    logger.info("Web UI listening on http://%s:%s", host, port)
    try:
        await server.serve()  # blocks until the process is signalled to stop
    finally:
        scheduler.shutdown(wait=False)
        await engine.dispose()


def main() -> None:
    """Load config, run migrations, and start the blocking scheduler."""
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_migrations(settings)
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
