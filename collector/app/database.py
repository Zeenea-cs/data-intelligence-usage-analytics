"""Database engine and session factory.

Provides the SQLAlchemy 2.x async engine and an ``async_sessionmaker`` used by
collectors. All writes must be wrapped in transactions; ``session_scope`` does
this for callers by committing on success and rolling back on error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_db_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create and return a SQLAlchemy async engine for the given database URL.

    Args:
        database_url: A ``postgresql+psycopg://`` URL (see ``Settings.database_url``).
        echo: When True, log all emitted SQL. Defaults to False.
    """
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


async def create_schema(engine: AsyncEngine) -> None:
    """Create all tables from the ORM metadata if they do not exist.

    This is a 1.0 application with no deployment history, so the schema is built
    directly from the models at startup -- there are no migrations. ``create_all``
    is idempotent (it skips tables that already exist).
    """
    # Imported here to avoid a circular import (models imports nothing from here).
    from app.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return an ``async_sessionmaker`` bound to the given engine."""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a transactional async session.

    Commits on clean exit and rolls back if the block raises. The session is
    always closed.
    """
    session = session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def upsert_rows(
    session: AsyncSession,
    table: Table,
    rows: Sequence[dict[str, Any]],
    *,
    index_elements: Sequence[str],
    update_columns: Sequence[str],
) -> int:
    """Bulk upsert rows via INSERT ... ON CONFLICT DO UPDATE.

    Uses the PostgreSQL dialect in production and the SQLite dialect under test;
    both support ``on_conflict_do_update``. Columns not listed in
    ``update_columns`` (e.g. ``first_seen_at``) are preserved on conflict.

    Args:
        table: target table (e.g. ``User.__table__``).
        rows: mappings to insert; empty is a no-op.
        index_elements: conflict-target columns (the unique/PK key).
        update_columns: columns to overwrite when a conflicting row exists.

    Returns:
        The number of rows processed.
    """
    if not rows:
        return 0

    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:
        raise RuntimeError(f"upsert_rows does not support dialect {dialect!r}")

    stmt = insert(table).values(list(rows))
    stmt = stmt.on_conflict_do_update(
        index_elements=list(index_elements),
        set_={col: getattr(stmt.excluded, col) for col in update_columns},
    )
    await session.execute(stmt)
    return len(rows)
