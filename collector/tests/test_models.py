"""Tests for ORM models: table names and column definitions."""

from __future__ import annotations

from app.models import AuditEvent, Base, CollectionRun, Item, User


def test_user_table_name() -> None:
    """User maps to the 'users' table with the expected columns."""
    assert User.__tablename__ == "users"
    cols = set(User.__table__.columns.keys())
    assert {
        "id",
        "username",
        "email",
        "display_name",
        "is_steward",
        "roles",
        "attributes",
        "first_seen_at",
        "last_updated_at",
    } == cols
    assert User.__table__.c.id.primary_key


def test_audit_event_table_name() -> None:
    """AuditEvent maps to 'audit_events' with a unique event_id."""
    assert AuditEvent.__tablename__ == "audit_events"
    assert AuditEvent.__table__.c.event_id.unique
    assert not AuditEvent.__table__.c.event_id.nullable


def test_item_and_collection_run_tables() -> None:
    """Item and CollectionRun map to their documented table names."""
    assert Item.__tablename__ == "items"
    assert CollectionRun.__tablename__ == "collection_runs"


def test_all_four_tables_registered() -> None:
    """Base metadata registers exactly the four documented tables."""
    assert set(Base.metadata.tables.keys()) == {
        "users",
        "audit_events",
        "items",
        "collection_runs",
    }
