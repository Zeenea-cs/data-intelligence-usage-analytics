"""Programmatic Metabase setup.

Idempotent. Waits for Metabase health, completes the setup wizard (or logs in if
already set up), connects the `actian_companion` database, creates a set of
saved questions (Metabase "cards"), and assembles them into two example
dashboards for users to build on. Safe to re-run.

Configuration (env / .env, with safe defaults):
    METABASE_URL            default http://localhost:3000
    METABASE_ADMIN_EMAIL    default admin@actian-companion.local
    METABASE_ADMIN_PASSWORD default Actian-Companion-2026
    METABASE_SITE_NAME      default Actian Data Intelligence Companion
    POSTGRES_HOST/PORT/DB/USER/PASSWORD  (companion DB; same vars as the collector)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

import httpx
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("setup_metabase")

DB_DISPLAY_NAME = "Actian Companion"

# --------------------------------------------------------------------------- #
# Card definitions. `key` is used for dashboard layout references.
# --------------------------------------------------------------------------- #

CARDS: list[dict[str, Any]] = [
    {
        "key": "top_contributors",
        "name": "Top Contributors (last 30 days)",
        "display": "row",
        "sql": (
            "SELECT COALESCE(username, user_id) AS contributor, "
            "COUNT(*) AS edits FROM audit_events "
            "WHERE user_id IS NOT NULL "
            "AND occurred_at >= NOW() - INTERVAL '30 days' "
            "GROUP BY 1 ORDER BY edits DESC LIMIT 20"
        ),
        "viz": {"graph.dimensions": ["contributor"], "graph.metrics": ["edits"]},
    },
    {
        "key": "top_modified_items",
        "name": "Top Modified Items (last 30 days)",
        "display": "table",
        "sql": (
            # item_type comes from the Catalog (items table), not the audit
            # trail (where it is always 'Item').
            "SELECT COALESCE(NULLIF(i.name, ''), NULLIF(ae.item_name, ''), "
            "ae.item_id) AS item_name, "
            "COALESCE(i.item_type, '(unknown)') AS item_type, "
            "COUNT(*) AS modifications "
            "FROM audit_events ae "
            "LEFT JOIN items i ON i.id = ae.item_id "
            "WHERE ae.user_id IS NOT NULL "
            "AND ae.occurred_at >= NOW() - INTERVAL '30 days' "
            "GROUP BY ae.item_id, i.name, ae.item_name, i.item_type "
            "ORDER BY modifications DESC LIMIT 20"
        ),
        "viz": {},
    },
    {
        "key": "weekly_pace",
        "name": "Weekly Documentation Pace",
        "display": "line",
        "sql": (
            "SELECT DATE_TRUNC('week', occurred_at) AS week, COUNT(*) AS events "
            "FROM audit_events WHERE user_id IS NOT NULL "
            "GROUP BY week ORDER BY week"
        ),
        "viz": {"graph.dimensions": ["week"], "graph.metrics": ["events"]},
    },
    {
        "key": "events_by_action",
        "name": "Events by Action",
        "display": "bar",
        "sql": (
            "SELECT action, COUNT(*) AS events FROM audit_events "
            "WHERE user_id IS NOT NULL GROUP BY action ORDER BY events DESC"
        ),
        "viz": {"graph.dimensions": ["action"], "graph.metrics": ["events"]},
    },
    {
        "key": "daily_activity",
        "name": "Daily Activity (last 30 days)",
        "display": "line",
        "sql": (
            "SELECT DATE_TRUNC('day', occurred_at) AS day, COUNT(*) AS events "
            "FROM audit_events WHERE user_id IS NOT NULL "
            "AND occurred_at >= NOW() - INTERVAL '30 days' "
            "GROUP BY day ORDER BY day"
        ),
        "viz": {"graph.dimensions": ["day"], "graph.metrics": ["events"]},
    },
    {
        "key": "stewards_breakdown",
        "name": "Stewards vs Non-stewards",
        "display": "pie",
        "sql": (
            "SELECT CASE WHEN is_steward THEN 'Steward' ELSE 'Non-steward' END "
            "AS user_type, COUNT(*) AS users FROM users GROUP BY 1 ORDER BY users DESC"
        ),
        "viz": {"pie.dimension": "user_type", "pie.metric": "users"},
    },
    {
        "key": "top_users_by_logins",
        "name": "Most Active Users (by login count)",
        "display": "row",
        "sql": (
            "SELECT COALESCE(display_name, username) AS user_name, "
            "NULLIF(attributes->>'Logins count', '')::int AS logins FROM users "
            "WHERE attributes->>'Logins count' IS NOT NULL "
            "ORDER BY logins DESC NULLS LAST LIMIT 15"
        ),
        "viz": {"graph.dimensions": ["user_name"], "graph.metrics": ["logins"]},
    },
    {
        "key": "users_by_permission_set",
        "name": "Users by Permission Set",
        "display": "row",
        "sql": (
            "SELECT COALESCE(roles->>'name', '(none)') AS permission_set, "
            "COUNT(*) AS users FROM users GROUP BY 1 ORDER BY users DESC"
        ),
        "viz": {"graph.dimensions": ["permission_set"], "graph.metrics": ["users"]},
    },
    # --- "Most Active Users" report -------------------------------------- #
    # Activity = number of audit events authored by a known user (audit
    # user_id joined to users). Most/least active per rolling window; least
    # active is ranked among users with >= 1 event in the window.
    *[
        {
            "key": f"mau_{rank}_{label}",
            "name": f"{'Most' if rank == 'top' else 'Least'} Active Users ({label})",
            "display": "row",
            "sql": (
                "SELECT COALESCE(u.display_name, ae.username, ae.user_id) AS user_name, "
                "COUNT(*) AS events "
                "FROM audit_events ae LEFT JOIN users u ON u.id = ae.user_id "
                "WHERE ae.user_id IS NOT NULL "
                f"AND ae.occurred_at >= NOW() - INTERVAL '{days} days' "
                f"GROUP BY 1 ORDER BY events {direction}, user_name LIMIT 10"
            ),
            "viz": {"graph.dimensions": ["user_name"], "graph.metrics": ["events"]},
        }
        for label, days in (("last 7 days", 7), ("last 30 days", 30), ("last 365 days", 365))
        for rank, direction in (("top", "DESC"), ("low", "ASC"))
    ],
    {
        # Comprehensive list of all users with their all-time activity (0 included).
        "key": "mau_all_users",
        "name": "All Users — Activity Summary",
        "display": "table",
        "sql": (
            "SELECT COALESCE(u.display_name, u.username) AS user_name, u.email, "
            "u.is_steward, COUNT(ae.event_id) AS total_events, "
            "MAX(ae.occurred_at) AS last_activity "
            "FROM users u LEFT JOIN audit_events ae ON ae.user_id = u.id "
            "GROUP BY u.id, u.display_name, u.username, u.email, u.is_steward "
            "ORDER BY total_events DESC"
        ),
        "viz": {},
    },
    # --- "Most Updated Items" report ------------------------------------- #
    {
        "key": "mui_top10",
        "name": "Most Updated Items (top 10)",
        "display": "row",
        "sql": (
            "SELECT COALESCE(item_name, item_id) AS item, COUNT(*) AS modifications "
            "FROM audit_events WHERE user_id IS NOT NULL AND item_id IS NOT NULL "
            "GROUP BY item_id, item_name ORDER BY modifications DESC LIMIT 10"
        ),
        "viz": {"graph.dimensions": ["item"], "graph.metrics": ["modifications"]},
    },
    {
        "key": "mui_table",
        "name": "Most Updated Items (detail)",
        "display": "table",
        "sql": (
            # item_type from the Catalog (items table), not the audit trail.
            "SELECT COALESCE(i.name, ae.item_name, ae.item_id) AS item, "
            "COALESCE(i.item_type, '(unknown)') AS item_type, "
            "COUNT(*) AS modifications, MAX(ae.occurred_at) AS last_modified "
            "FROM audit_events ae LEFT JOIN items i ON i.id = ae.item_id "
            "WHERE ae.user_id IS NOT NULL AND ae.item_id IS NOT NULL "
            "GROUP BY ae.item_id, i.name, ae.item_name, i.item_type "
            "ORDER BY modifications DESC LIMIT 25"
        ),
        "viz": {},
    },
    # --- "Documentation coverage per curator" report --------------------- #
    # coverage_ratio = (managed items the curator has an edit event on) /
    # (items the curator manages). Managed = items.owner_id is the curator.
    {
        "key": "dcc_ratio",
        "name": "Documentation Coverage Ratio per Curator",
        "display": "row",
        "sql": (
            "WITH edited AS ("
            " SELECT DISTINCT user_id, item_id FROM audit_events "
            " WHERE user_id IS NOT NULL AND item_id IS NOT NULL "
            " AND action = 'UpdateItem') "
            "SELECT COALESCE(i.owner_name, i.owner_email, i.owner_id) AS curator, "
            "ROUND(COUNT(e.item_id)::numeric / NULLIF(COUNT(*), 0), 3) AS coverage_ratio "
            "FROM items i "
            "LEFT JOIN edited e ON e.user_id = i.owner_id AND e.item_id = i.id "
            "WHERE i.owner_id IS NOT NULL "
            "GROUP BY i.owner_id, COALESCE(i.owner_name, i.owner_email, i.owner_id) "
            "ORDER BY coverage_ratio DESC"
        ),
        "viz": {"graph.dimensions": ["curator"], "graph.metrics": ["coverage_ratio"]},
    },
    {
        "key": "dcc_table",
        "name": "Documentation Coverage per Curator (detail)",
        "display": "table",
        "sql": (
            "WITH edited AS ("
            " SELECT DISTINCT user_id, item_id FROM audit_events "
            " WHERE user_id IS NOT NULL AND item_id IS NOT NULL "
            " AND action = 'UpdateItem') "
            "SELECT COALESCE(i.owner_name, i.owner_email, i.owner_id) AS curator, "
            "COUNT(*) AS managed_items, COUNT(e.item_id) AS edited_items, "
            "ROUND(COUNT(e.item_id)::numeric / NULLIF(COUNT(*), 0), 3) AS coverage_ratio "
            "FROM items i "
            "LEFT JOIN edited e ON e.user_id = i.owner_id AND e.item_id = i.id "
            "WHERE i.owner_id IS NOT NULL "
            "GROUP BY i.owner_id, COALESCE(i.owner_name, i.owner_email, i.owner_id) "
            "ORDER BY coverage_ratio DESC"
        ),
        "viz": {},
    },
]

# Dashboards reference cards by key with a 24-column grid layout.
DASHBOARDS: list[dict[str, Any]] = [
    {
        "name": "Actian Data Intelligence Activity",
        "description": "Example dashboard: catalog editing activity from audit events.",
        "layout": [
            ("top_contributors", 0, 0, 12, 8),
            ("top_modified_items", 0, 12, 12, 8),
            ("weekly_pace", 8, 0, 12, 8),
            ("events_by_action", 8, 12, 12, 8),
            ("daily_activity", 16, 0, 24, 8),
        ],
    },
    {
        "name": "Users & Stewardship",
        "description": "Example dashboard: user population, stewardship and activity.",
        "layout": [
            ("stewards_breakdown", 0, 0, 8, 8),
            ("users_by_permission_set", 0, 8, 16, 8),
            ("top_users_by_logins", 8, 0, 24, 8),
        ],
    },
    {
        "name": "Most Active Users",
        "description": "User activity from audit events: most/least active per "
        "rolling week, month and year, plus all users.",
        "layout": [
            ("mau_top_last 7 days", 0, 0, 12, 8),
            ("mau_low_last 7 days", 0, 12, 12, 8),
            ("mau_top_last 30 days", 8, 0, 12, 8),
            ("mau_low_last 30 days", 8, 12, 12, 8),
            ("mau_top_last 365 days", 16, 0, 12, 8),
            ("mau_low_last 365 days", 16, 12, 12, 8),
            ("mau_all_users", 24, 0, 24, 9),
        ],
    },
    {
        "name": "Most Updated Items",
        "description": "Items ranked by number of modifications in the audit trail.",
        "layout": [
            ("mui_top10", 0, 0, 12, 8),
            ("mui_table", 0, 12, 12, 9),
        ],
    },
    {
        "name": "Documentation Coverage per Curator",
        "description": "Per curator: edited managed items / managed items.",
        "layout": [
            ("dcc_ratio", 0, 0, 24, 8),
            ("dcc_table", 8, 0, 24, 8),
        ],
    },
]


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def wait_for_health(client: httpx.Client, *, attempts: int = 60, delay: float = 2.0) -> None:
    """Poll /api/health until Metabase reports ok."""
    for _ in range(attempts):
        try:
            resp = client.get("/api/health")
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                logger.info("Metabase is healthy")
                return
        except httpx.HTTPError:
            pass
        time.sleep(delay)
    raise RuntimeError("Metabase did not become healthy in time")


def complete_setup_wizard(client: httpx.Client, *, email: str, password: str, site_name: str) -> str:
    """Complete the setup wizard if needed, else log in. Return a session token."""
    props = client.get("/api/session/properties").json()
    setup_token = props.get("setup-token")

    # `has-user-setup` is the reliable signal: a setup-token may still be
    # returned after setup, but re-running /api/setup then 403s.
    if not props.get("has-user-setup") and setup_token:
        logger.info("Running first-time setup wizard")
        resp = client.post(
            "/api/setup",
            json={
                "token": setup_token,
                "user": {
                    "first_name": "Actian",
                    "last_name": "Admin",
                    "email": email,
                    "password": password,
                    "site_name": site_name,
                },
                "prefs": {"site_name": site_name, "allow_tracking": False},
            },
        )
        resp.raise_for_status()
        return resp.json()["id"]

    logger.info("Metabase already set up; logging in")
    resp = client.post("/api/session", json={"username": email, "password": password})
    resp.raise_for_status()
    return resp.json()["id"]


# Metabase's default example assets (removed so only app content remains).
EXAMPLE_DASHBOARD_NAMES = {"E-commerce Insights"}
EXAMPLE_COLLECTION_NAMES = {"Examples"}


def remove_example_content(client: httpx.Client) -> None:
    """Remove Metabase's built-in example assets, leaving only app content.

    Deletes the Sample Database and archives the default example dashboard and
    the "Examples" collection (which archives the sample questions inside it).
    Idempotent -- skips anything already absent/archived.
    """
    databases = client.get("/api/database").json()
    databases = databases["data"] if isinstance(databases, dict) else databases
    for db in databases:
        if db.get("is_sample") or db.get("name") == "Sample Database":
            client.delete(f"/api/database/{db['id']}").raise_for_status()
            logger.info("Removed sample database (id=%s)", db["id"])

    for dash in client.get("/api/dashboard").json():
        if dash.get("name") in EXAMPLE_DASHBOARD_NAMES and not dash.get("archived"):
            client.put(f"/api/dashboard/{dash['id']}", json={"archived": True}).raise_for_status()
            logger.info("Archived example dashboard: %s", dash["name"])

    for coll in client.get("/api/collection").json():
        if coll.get("name") in EXAMPLE_COLLECTION_NAMES and isinstance(coll.get("id"), int):
            client.put(f"/api/collection/{coll['id']}", json={"archived": True}).raise_for_status()
            logger.info("Archived example collection: %s", coll["name"])


def add_database_connection(client: httpx.Client) -> int:
    """Add (or find) the actian_companion database connection. Return its id."""
    existing = client.get("/api/database").json()
    databases = existing["data"] if isinstance(existing, dict) else existing
    for db in databases:
        if db.get("name") == DB_DISPLAY_NAME:
            logger.info("Database connection already exists (id=%s)", db["id"])
            return db["id"]

    resp = client.post(
        "/api/database",
        json={
            "engine": "postgres",
            "name": DB_DISPLAY_NAME,
            "details": {
                "host": _env("POSTGRES_HOST", "db"),
                "port": int(_env("POSTGRES_PORT", "5432")),
                "dbname": _env("POSTGRES_DB", "actian_companion"),
                "user": _env("POSTGRES_USER", "actian"),
                "password": _env("POSTGRES_PASSWORD", ""),
                "ssl": False,
            },
            "is_full_sync": True,
        },
    )
    resp.raise_for_status()
    db_id = resp.json()["id"]
    logger.info("Created database connection (id=%s)", db_id)
    return db_id


def create_cards(client: httpx.Client, database_id: int) -> dict[str, int]:
    """Create or update the saved questions (idempotent by name).

    Existing cards are updated (PUT) so SQL/display changes propagate on re-run
    while keeping the same card id (dashboards reference it). Returns key -> id.
    """
    existing = {c["name"]: c["id"] for c in client.get("/api/card").json()}
    key_to_id: dict[str, int] = {}
    for card in CARDS:
        body = {
            "name": card["name"],
            "dataset_query": {
                "type": "native",
                "native": {"query": card["sql"]},
                "database": database_id,
            },
            "display": card["display"],
            "visualization_settings": card["viz"],
        }
        if card["name"] in existing:
            card_id = existing[card["name"]]
            resp = client.put(f"/api/card/{card_id}", json=body)
            resp.raise_for_status()
            logger.info("Updated card: %s", card["name"])
        else:
            resp = client.post("/api/card", json=body)
            resp.raise_for_status()
            card_id = resp.json()["id"]
            logger.info("Created card: %s", card["name"])
        key_to_id[card["key"]] = card_id
    return key_to_id


def create_dashboard(client: httpx.Client, spec: dict[str, Any], card_ids: dict[str, int]) -> int:
    """Create (or find) a dashboard and set its cards. Return its id."""
    existing = client.get("/api/dashboard").json()
    dash_id = next((d["id"] for d in existing if d.get("name") == spec["name"]), None)
    if dash_id is None:
        resp = client.post(
            "/api/dashboard",
            json={"name": spec["name"], "description": spec["description"]},
        )
        resp.raise_for_status()
        dash_id = resp.json()["id"]
        logger.info("Created dashboard: %s (id=%s)", spec["name"], dash_id)
    else:
        logger.info("Dashboard exists: %s (id=%s)", spec["name"], dash_id)

    dashcards = [
        {
            "id": -(idx + 1),
            "card_id": card_ids[key],
            "row": row,
            "col": col,
            "size_x": size_x,
            "size_y": size_y,
            "series": [],
            "parameter_mappings": [],
            "visualization_settings": {},
        }
        for idx, (key, row, col, size_x, size_y) in enumerate(spec["layout"])
        if key in card_ids
    ]
    resp = client.put(f"/api/dashboard/{dash_id}", json={"dashcards": dashcards})
    resp.raise_for_status()
    logger.info("Set %s cards on dashboard %s", len(dashcards), spec["name"])
    return dash_id


def main() -> None:
    """Run the full idempotent Metabase setup sequence."""
    load_dotenv()
    base_url = _env("METABASE_URL", "http://localhost:3000")
    email = _env("METABASE_ADMIN_EMAIL", "admin@actian-companion.local")
    password = _env("METABASE_ADMIN_PASSWORD", "Actian-Companion-2026")
    site_name = _env("METABASE_SITE_NAME", "Actian Data Intelligence Companion")

    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        wait_for_health(client)
        session = complete_setup_wizard(
            client, email=email, password=password, site_name=site_name
        )
        client.headers["X-Metabase-Session"] = session

        remove_example_content(client)
        database_id = add_database_connection(client)
        card_ids = create_cards(client, database_id)
        for spec in DASHBOARDS:
            create_dashboard(client, spec, card_ids)

    logger.info("Metabase setup complete. Open %s", base_url)
    logger.info("Admin login: %s", email)


if __name__ == "__main__":
    try:
        main()
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        logger.error("Setup failed: %s", exc)
        sys.exit(1)
