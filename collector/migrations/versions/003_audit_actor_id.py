"""Add audit_events.actor_id: the user behind an event, current or former.

``user_id`` is a foreign key to ``users``, so the collector leaves it NULL for
any actor absent from the latest user export. Every activity card filtered on
``user_id IS NOT NULL``, so a user who left before the first collection -- or
before a Force reload -- disappeared from every statistic, however large their
share of past edits.

``actor_id`` is generated from ``raw_payload`` (``origin.id`` when
``origin.originType == 'User'``), so it needs no collector change and is filled
in for existing rows by this migration.

Revision ID: 003
Revises: 002
Create Date: 2026-09-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Frozen copy of app.models.ACTOR_ID_SQL: a migration must not change meaning if
# the model's expression is edited later.
ACTOR_ID_SQL = (
    "CASE WHEN raw_payload->'origin'->>'originType' = 'User' "
    "THEN raw_payload->'origin'->>'id' END"
)

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column("actor_id", sa.String(255), sa.Computed(ACTOR_ID_SQL, persisted=True)),
    )
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_actor_id", table_name="audit_events")
    op.drop_column("audit_events", "actor_id")
