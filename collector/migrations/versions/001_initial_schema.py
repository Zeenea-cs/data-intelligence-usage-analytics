"""Initial schema: users, audit_events, items, collection_runs.

Mirrors the ORM models in ``app.models`` (the single source of truth). JSON
columns use PostgreSQL JSONB; the surrogate keys are BIGSERIAL.

Revision ID: 001
Revises:
Create Date: 2026-06-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# TIMESTAMPTZ shorthand.
_TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("username", sa.String(255)),
        sa.Column("email", sa.String(255)),
        sa.Column("display_name", sa.String(255)),
        sa.Column(
            "is_steward",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("roles", postgresql.JSONB()),
        sa.Column("attributes", postgresql.JSONB()),
        sa.Column("first_seen_at", _TZ),
        sa.Column("last_updated_at", _TZ),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(255), nullable=False, unique=True),
        sa.Column("user_id", sa.String(255), sa.ForeignKey("users.id")),
        sa.Column("username", sa.String(255)),
        sa.Column("action", sa.String(255)),
        sa.Column("item_id", sa.String(255)),
        sa.Column("item_type", sa.String(255)),
        sa.Column("item_name", sa.String(255)),
        sa.Column("occurred_at", _TZ),
        sa.Column("raw_payload", postgresql.JSONB()),
        sa.Column("collected_at", _TZ),
    )

    op.create_table(
        "items",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("key", sa.String(512)),
        sa.Column("item_type", sa.String(255)),
        sa.Column("name", sa.String(255)),
        sa.Column("description", sa.Text()),
        sa.Column("description_type", sa.String(50)),
        sa.Column("owner_id", sa.String(255)),
        sa.Column("owner_name", sa.String(255)),
        sa.Column("owner_email", sa.String(255)),
        sa.Column("attributes", postgresql.JSONB()),
        sa.Column("first_seen_at", _TZ),
        sa.Column("last_updated_at", _TZ),
    )

    op.create_table(
        "collection_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("started_at", _TZ),
        sa.Column("finished_at", _TZ),
        sa.Column("status", sa.String(50)),
        sa.Column("users_collected", sa.Integer()),
        sa.Column("events_collected", sa.Integer()),
        sa.Column("items_collected", sa.Integer()),
        sa.Column("error_message", sa.Text()),
    )


def downgrade() -> None:
    # Reverse order: audit_events references users.
    op.drop_table("collection_runs")
    op.drop_table("items")
    op.drop_table("audit_events")
    op.drop_table("users")
