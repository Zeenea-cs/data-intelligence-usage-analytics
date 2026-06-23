"""Add user_snapshots: append-only history of the user export.

Mirrors the ``UserSnapshot`` ORM model in ``app.models``. Each collection run
inserts one row per exported user (never updated), stamped with ``snapshot_at``,
so licence consumption (stewards vs explorers) can be trended over time.

Revision ID: 002
Revises: 001
Create Date: 2026-06-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# TIMESTAMPTZ shorthand.
_TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "user_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("snapshot_at", _TZ),
        sa.Column("collection_run_id", sa.BigInteger()),
        sa.Column("user_id", sa.String(255)),
        sa.Column("username", sa.String(255)),
        sa.Column("email", sa.String(255)),
        sa.Column("display_name", sa.String(255)),
        sa.Column(
            "is_steward",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("license_type", sa.String(255)),
        sa.Column("roles", postgresql.JSONB()),
        sa.Column("attributes", postgresql.JSONB()),
    )
    op.create_index(
        "ix_user_snapshots_snapshot_at", "user_snapshots", ["snapshot_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_snapshots_snapshot_at", table_name="user_snapshots")
    op.drop_table("user_snapshots")
