"""Alembic migration environment.

The URL is normally injected by ``app.migrate`` (startup) on the Config object.
For ad-hoc CLI use it falls back to the application settings, so
``alembic upgrade head`` works from the collector/ directory too.

Migrations run against a synchronous engine; the ``postgresql+psycopg`` URL
(psycopg 3) works in both sync (here) and async (the collector) modes.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate / comparison target.
target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the URL from the Config, else from application settings."""
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from app.config import load_settings

    return load_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (``--sql`` mode)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database via a synchronous engine."""
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
