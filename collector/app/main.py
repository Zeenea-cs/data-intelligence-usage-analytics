"""Collector entrypoint.

Startup sequence:
1. load + validate settings;
2. create the database schema from the ORM metadata (1.0 -- no migrations);
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
from app.database import create_db_engine, create_schema, create_session_factory
from app.scheduler import build_scheduler, run_collection
from app.webui import create_app

logger = logging.getLogger(__name__)

INITIAL_JOB_ID = "initial-collection"


async def _serve(settings: Settings) -> None:
    """Create the schema, start the scheduler + immediate run, and serve the UI."""
    engine = create_db_engine(settings.database_url)
    await create_schema(engine)
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
    """Load config, configure logging, and run the async service."""
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
