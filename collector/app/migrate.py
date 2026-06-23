"""Run Alembic migrations programmatically at startup.

CLAUDE.md mandates Alembic for schema management ("always use Alembic"), and the
collector must upgrade the database to ``head`` before scheduling begins. This
module wires an Alembic ``Config`` to the migrations directory and the runtime
database URL so ``main`` can call :func:`run_migrations` without shelling out.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)

# collector/migrations -- sibling of the app package (this file is app/migrate.py).
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _alembic_config(database_url: str) -> Config:
    """Build an Alembic Config pointed at our migrations dir and database URL."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    # Alembic interpolates "%" in URLs; escape any (e.g. percent-encoded passwords).
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def run_migrations(database_url: str) -> None:
    """Upgrade the database to ``head`` (idempotent; a no-op when already current)."""
    logger.info("Running Alembic migrations to head")
    command.upgrade(_alembic_config(database_url), "head")
    logger.info("Database schema is up to date")
