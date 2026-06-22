"""Add Catalog enrichment columns to items.

Revision ID: 002_items_catalog_fields
Revises: 001_initial_schema
Create Date:

Adds key, description_type, owner_name, owner_email to the `items` table for the
Catalog enrichment feature (DISCOVERY.md §4). owner_id / description already
exist from the initial schema.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002_items_catalog_fields"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the new Catalog item columns."""
    op.add_column("items", sa.Column("key", sa.String(length=512)))
    op.add_column("items", sa.Column("description_type", sa.String(length=50)))
    op.add_column("items", sa.Column("owner_name", sa.String(length=255)))
    op.add_column("items", sa.Column("owner_email", sa.String(length=255)))


def downgrade() -> None:
    """Drop the Catalog item columns."""
    op.drop_column("items", "owner_email")
    op.drop_column("items", "owner_name")
    op.drop_column("items", "description_type")
    op.drop_column("items", "key")
