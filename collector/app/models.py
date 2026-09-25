"""SQLAlchemy ORM models -- the single source of truth for the schema.

Four tables: users, audit_events, items, collection_runs. These models are the
source of truth for the schema; the Alembic migrations under ``migrations/``
must be kept in step with them (the collector runs them to head at startup --
see app.migrate). The initial schema is migration ``001_initial_schema``.

The JSONB / BigInteger columns use dialect variants so the same models build on
PostgreSQL (production) and SQLite (tests).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Computed,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# TIMESTAMPTZ helper.
_TZ = DateTime(timezone=True)

# JSONB on PostgreSQL (production); plain JSON on SQLite (tests).
_JSONB = JSON().with_variant(JSONB(), "postgresql")

# The user behind an audit event, whether or not they are still a platform user.
# Valid on both PostgreSQL and SQLite (3.38+), so tests build the same column.
ACTOR_ID_SQL = (
    "CASE WHEN raw_payload->'origin'->>'originType' = 'User' "
    "THEN raw_payload->'origin'->>'id' END"
)

# BIGSERIAL/identity on PostgreSQL; INTEGER rowid (autoincrement) on SQLite, so
# surrogate primary keys are generated under both.
_BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class User(Base):
    """A user discovered via the User Management / SCIM APIs.

    Maps to the `users` table.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_steward: Mapped[bool] = mapped_column(Boolean, default=False)
    roles: Mapped[Any | None] = mapped_column(_JSONB)
    attributes: Mapped[Any | None] = mapped_column(_JSONB)
    first_seen_at: Mapped[datetime | None] = mapped_column(_TZ)
    last_updated_at: Mapped[datetime | None] = mapped_column(_TZ)


class UserSnapshot(Base):
    """An append-only snapshot of one user as seen in a single export run.

    Maps to the `user_snapshots` table. Unlike `users` (upserted, one live row
    per user), this table is never updated: every collection run inserts a fresh
    row per exported user, stamped with `snapshot_at`. This preserves the full
    user export over time so licence consumption trends (stewards vs explorers)
    can be analysed historically. Preserved across "Force reload" (like
    `collection_runs`).
    """

    __tablename__ = "user_snapshots"

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    snapshot_at: Mapped[datetime | None] = mapped_column(_TZ, index=True)
    collection_run_id: Mapped[int | None] = mapped_column(_BIGINT_PK)
    user_id: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_steward: Mapped[bool] = mapped_column(Boolean, default=False)
    license_type: Mapped[str | None] = mapped_column(String(255))
    roles: Mapped[Any | None] = mapped_column(_JSONB)
    attributes: Mapped[Any | None] = mapped_column(_JSONB)


class AuditEvent(Base):
    """A single audit event from the Audit API.

    Maps to the `audit_events` table.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("users.id")
    )
    # user_id is only set for users present in the latest export (it is a foreign
    # key), so a user who left before the first collection -- or before a Force
    # reload -- had every edit dropped from the activity statistics. actor_id is
    # derived from the payload instead, so it always names the user behind the
    # event, current or former. Generated, so no collector writes it and existing
    # rows are filled in by the migration.
    actor_id: Mapped[str | None] = mapped_column(
        String(255), Computed(ACTOR_ID_SQL, persisted=True), index=True
    )
    username: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str | None] = mapped_column(String(255))
    item_id: Mapped[str | None] = mapped_column(String(255))
    item_type: Mapped[str | None] = mapped_column(String(255))
    item_name: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime | None] = mapped_column(_TZ)
    raw_payload: Mapped[Any | None] = mapped_column(_JSONB)
    collected_at: Mapped[datetime | None] = mapped_column(_TZ)


class Item(Base):
    """A catalog item (dataset, field, glossary, etc.).

    Maps to the `items` table. Phase 2 scope.
    """

    __tablename__ = "items"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    key: Mapped[str | None] = mapped_column(String(512))
    item_type: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    description_type: Mapped[str | None] = mapped_column(String(50))
    owner_id: Mapped[str | None] = mapped_column(String(255))
    owner_name: Mapped[str | None] = mapped_column(String(255))
    owner_email: Mapped[str | None] = mapped_column(String(255))
    attributes: Mapped[Any | None] = mapped_column(_JSONB)
    first_seen_at: Mapped[datetime | None] = mapped_column(_TZ)
    last_updated_at: Mapped[datetime | None] = mapped_column(_TZ)


class CollectionRun(Base):
    """A record of one collection run and its outcome.

    Maps to the `collection_runs` table.
    """

    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime | None] = mapped_column(_TZ)
    finished_at: Mapped[datetime | None] = mapped_column(_TZ)
    status: Mapped[str | None] = mapped_column(String(50))
    users_collected: Mapped[int | None] = mapped_column(Integer)
    events_collected: Mapped[int | None] = mapped_column(Integer)
    items_collected: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
