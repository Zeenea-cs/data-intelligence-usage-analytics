"""Initial schema: users, audit_events, items, collection_runs.

Revision ID: 001_initial_schema
Revises:
Create Date:

Creates all four tables defined in CLAUDE.md. Do not use CREATE TABLE IF NOT
EXISTS -- schema is owned by Alembic.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

_TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create users, audit_events, items, and collection_runs tables."""
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=255), primary_key=True),
        sa.Column("username", sa.String(length=255)),
        sa.Column("email", sa.String(length=255)),
        sa.Column("display_name", sa.String(length=255)),
        sa.Column("is_steward", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("roles", postgresql.JSONB()),
        sa.Column("attributes", postgresql.JSONB()),
        sa.Column("first_seen_at", _TZ),
        sa.Column("last_updated_at", _TZ),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=255), sa.ForeignKey("users.id")),
        sa.Column("username", sa.String(length=255)),
        sa.Column("action", sa.String(length=255)),
        sa.Column("item_id", sa.String(length=255)),
        sa.Column("item_type", sa.String(length=255)),
        sa.Column("item_name", sa.String(length=255)),
        sa.Column("occurred_at", _TZ),
        sa.Column("raw_payload", postgresql.JSONB()),
        sa.Column("collected_at", _TZ),
        sa.UniqueConstraint("event_id", name="uq_audit_events_event_id"),
    )

    op.create_table(
        "items",
        sa.Column("id", sa.String(length=255), primary_key=True),
        sa.Column("item_type", sa.String(length=255)),
        sa.Column("name", sa.String(length=255)),
        sa.Column("description", sa.Text()),
        sa.Column("owner_id", sa.String(length=255)),
        sa.Column("attributes", postgresql.JSONB()),
        sa.Column("first_seen_at", _TZ),
        sa.Column("last_updated_at", _TZ),
    )

    op.create_table(
        "collection_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("started_at", _TZ),
        sa.Column("finished_at", _TZ),
        sa.Column("status", sa.String(length=50)),
        sa.Column("users_collected", sa.Integer()),
        sa.Column("events_collected", sa.Integer()),
        sa.Column("items_collected", sa.Integer()),
        sa.Column("error_message", sa.Text()),
    )


def downgrade() -> None:
    """Drop all tables created in upgrade()."""
    op.drop_table("collection_runs")
    op.drop_table("items")
    op.drop_table("audit_events")
    op.drop_table("users")
