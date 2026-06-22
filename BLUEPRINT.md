# Build Prompt — Actian Data Intelligence Companion

This document is a complete, self-contained specification to recreate the
application from scratch with the exact same features. Treat it as the
authoritative prompt: build precisely what is described. It includes the
**discovered API contracts** (verified against a live instance) so the API
clients can be implemented without re-running discovery.

---

## 0. Goal

Build a Dockerised analytics companion for the **Actian Data Intelligence
Platform** (Zeenea SaaS Data Catalog). A Python service ("the collector")
periodically pulls data from four Actian APIs into PostgreSQL; **Metabase**
exposes it via example dashboards; a small **web UI** triggers collection on
demand. Everything runs via Docker Compose.

---

## 1. Technology choices (do not deviate)

- Collector: **Python 3.12**, **APScheduler** (cron), **httpx** (async HTTP).
- Database: **PostgreSQL 16**.
- ORM: **SQLAlchemy 2.x (async)**. No migration tool — this is a 1.0 with no
  deployment history, so the schema is created from the ORM metadata at startup.
- Web UI: **FastAPI** + **uvicorn**, served by the collector process in the same
  asyncio event loop as the scheduler.
- Reporting: **Metabase** (latest stable Docker image).
- Orchestration: **Docker Compose v2**.
- Config: **python-dotenv**; all secrets via environment variables.
- Testing: **pytest** + **respx** (httpx mock) + **aiosqlite** (in-memory DB).
- Lint: **ruff** (zero errors). Type hints throughout. No hardcoded URLs,
  credentials, or magic strings outside `config.py` (relative API paths may be
  module constants in their client).
- Runtime deps also include **greenlet** (SQLAlchemy async) and
  **psycopg[binary]** (Postgres driver; the `postgresql+psycopg` dialect serves
  the async collectors).

---

## 2. Repository structure

```
actian-companion/
  collector/
    app/
      __init__.py
      config.py            # env load + validation (incl. web UI host/port)
      database.py          # async engine, session factory, schema create, upsert helper
      models.py            # SQLAlchemy models (the schema's single source of truth)
      timeutils.py         # ISO-8601 parse + format (handles Z + nanoseconds)
      clients.py           # factory bundling the three API clients from Settings
      logsetup.py          # console + rotating-file logging for the collector
      logs.py              # per-service log file map + safe tail (web UI)
      api/
        __init__.py        # ApiError
        _http.py           # shared headers, request/GraphQL helpers, error wrapping
        audit.py           # Audit REST client
        catalog.py         # Catalog GraphQL client
        users.py           # User Management (GraphQL) + SCIM (REST) client
      collectors/
        __init__.py
        users.py
        audit.py
        items.py
      scheduler.py         # AsyncIOScheduler + collection cycle + run record
      webui.py             # FastAPI trigger UI
      main.py              # entrypoint: create schema -> scheduler + uvicorn
    tests/
      test_config.py  test_models.py  test_api_clients.py
      test_collectors.py  test_scheduler.py  test_items.py  test_webui.py
    requirements.txt
    Dockerfile
  scripts/
    discover_api.py        # interactive API discovery tool
    setup_metabase.py      # idempotent Metabase provisioning
  docker-compose.yml
  .env.example
  .gitignore
  README.md
  DISCOVERY.md             # the confirmed API contracts (source of truth)
```

---

## 3. Configuration (environment variables)

Read from environment / `.env`. Required have no default; `load_settings()`
raises `RuntimeError` listing every missing required var.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ACTIAN_INSTANCE_URL` | yes | — | Instance base URL, trailing slash stripped |
| `ACTIAN_API_KEY` | yes | — | API key (header `X-API-SECRET`; Bearer for SCIM) |
| `COLLECT_CRON` | no | `0 0 * * *` | Collection cron |
| `POSTGRES_HOST` | no | `db` | Companion DB host |
| `POSTGRES_PORT` | no | `5432` | Companion DB port (validated int) |
| `POSTGRES_DB` | no | `actian_companion` | Companion DB name |
| `POSTGRES_USER` | no | `actian` | Companion DB user |
| `POSTGRES_PASSWORD` | yes | — | Companion DB password |
| `METABASE_DB_PASSWORD` | yes | — | Metabase application DB password |
| `LOG_LEVEL` | no | `INFO` | Collector log level |
| `WEBUI_HOST` | no | `0.0.0.0` | Web UI bind host |
| `WEBUI_PORT` | no | `8000` | Web UI port |
| `LOG_DIR` | no | `/var/log/actian` | Shared log dir (bind-mounted to `./logs`) |
| `LOG_MAX_BYTES` | no | `5000000` | Collector log rotation size |
| `LOG_BACKUP_COUNT` | no | `5` | Rotated collector logs kept |
| `POSTGRES_LOG_MIN_MESSAGES` | no | `warning` | Postgres log verbosity (db services) |

`Settings` is a frozen dataclass with a `database_url` property returning
`postgresql+psycopg://USER:PASSWORD@HOST:PORT/DB`.

Setup-script-only vars (used by `scripts/setup_metabase.py`, all optional):
`METABASE_URL` (`http://localhost:3000`), `METABASE_ADMIN_EMAIL`
(`admin@actian-companion.local`), `METABASE_ADMIN_PASSWORD`
(`Actian-Companion-2026`), `METABASE_SITE_NAME`.

---

## 4. Data model (PostgreSQL)

The ORM models are the single source of truth; the schema is created from them
at startup via `Base.metadata.create_all` (1.0 — no migrations). JSONB columns in
Postgres (use `JSON().with_variant(JSONB, "postgresql")` in models so tests can
run on SQLite). BIGSERIAL PKs (`BigInteger().with_variant(Integer, "sqlite")`).
All timestamps `TIMESTAMPTZ` (`DateTime(timezone=True)`).

**users**: `id VARCHAR PK`, `username`, `email`, `display_name`,
`is_steward BOOL`, `roles JSONB`, `attributes JSONB`, `first_seen_at`,
`last_updated_at`.

**audit_events**: `id BIGSERIAL PK`, `event_id VARCHAR UNIQUE`,
`user_id VARCHAR FK->users.id`, `username`, `action`, `item_id`, `item_type`,
`item_name`, `occurred_at`, `raw_payload JSONB`, `collected_at`.

**items**: `id VARCHAR PK`, `key VARCHAR(512)`, `item_type`, `name`,
`description TEXT`, `description_type VARCHAR(50)`, `owner_id`, `owner_name`,
`owner_email`, `attributes JSONB`, `first_seen_at`, `last_updated_at`.

**collection_runs**: `id BIGSERIAL PK`, `started_at`, `finished_at`,
`status VARCHAR(50)`, `users_collected INT`, `events_collected INT`,
`items_collected INT`, `error_message TEXT`.

A reusable async `upsert_rows(session, table, rows, index_elements,
update_columns)` helper performs `INSERT ... ON CONFLICT DO UPDATE`, dispatching
to the postgresql or sqlite dialect insert by `session.get_bind().dialect.name`.
Columns not in `update_columns` (e.g. `first_seen_at`) are preserved on conflict.

---

## 5. API contracts (CONFIRMED against the live instance)

All four APIs share the same base URL and API key. Auth header is
`X-API-SECRET: {key}` on every request **except SCIM**, which uses
`Authorization: Bearer {key}`. REST and GraphQL request patterns must not be
mixed across clients. All HTTP/transport/GraphQL failures are logged and
re-raised as a single `ApiError` so a collector failure can be contained.

### 5.1 Audit API — REST
- `POST /public-api/management/audit` (POST, **not** GET). Headers:
  `X-API-SECRET`, `Content-Type: application/json`.
- Body: `{"eventType": "Item", "from": <iso>, "to": <iso>, "cursorMark": <c>}`.
  - `eventType` **required**, case-sensitive enum:
    `Item | User | Group | ApiKey | PermissionSet`. Collect **`Item`** only.
  - `from` and `to` are **required** (ISO-8601, e.g.
    `2026-06-12T00:00:00.000Z`); a missing bound → HTTP 500
    `error.path.missing`. When the caller passes no window, default to a
    look-back ending now (e.g. 365 days).
- **Pagination = cursor.** Response: `{"data": [...], "pagination":
  {"cursorMark": "..."}}`. Resend the same body with `cursorMark` from the prior
  response; stop when `data` is empty or the returned cursorMark is `""`.
  `pageSize` is ignored.
- `Item` event shape: `{ id, timestamp, eventType:"Item", origin:{id,
  originType}, value, previousValue?, itemId, itemName, itemEventType }`.
  Item events carry **no username** and no item-type beyond `eventType`.

### 5.2 SCIM API — REST
- `GET /api/scim/v2/Users`. Auth **`Authorization: Bearer {key}`** (NOT
  X-API-SECRET → 401). `Accept: application/scim+json`.
- **Pagination = `startIndex` (1-based) + `count`.** Response:
  `{ totalResults, itemsPerPage, startIndex, Resources:[...] }`. Stop when no
  resources remain.
- User resource: `{ id, userName, name:{givenName,familyName}, active,
  emails:[{value}], groups:[{value, display}] }`.

### 5.3 User Management API — GraphQL
- `POST /public-api/catalog/graphql` (`{"query","variables"}`). Auth
  `X-API-SECRET`. **Use only these three operations:**
  1. Mutation `createAllUsersExport { exportId }`.
  2. Query `loadUsersExportStatus(input:{exportId}) { exportId status url }` —
     poll until `status == DONE` (enum `RUNNING | DONE | ERROR`). On DONE, `url`
     is a presigned S3 link (≈1h) returning **CSV** (`text/csv`).
  3. Query `listPermissionSets { id name description builtIn licenseType
     permissions { permission } }` — resolves the export's `Permission set id`.
- There is **no list-users query**; enumeration is the CSV export.
- Export CSV header columns (exact): `Id, Email, First name, Last name, Phone,
  Permission set id, Permission set name, Permission set description, Permission
  set built-in, Custom item documentation permission, Custom item documentation
  scope, Item documentation permission, Item documentation scope, Glossary
  documentation permission, Glossary documentation scope, Catalog design
  permission, Ops administration permission, Users and permissions
  administration permission, Analytics dashboard permission, License type,
  Creation date, Last login, Logins count`. `License type` ∈ `Explorer |
  Steward`.

### 5.4 Catalog API — GraphQL
- `POST /api/catalog/graphql`. Auth `X-API-SECRET`. Introspection enabled.
- `item(ref: ItemReference!) -> Item` fetches one item by **UUID or key**
  (`ItemReference` is a scalar). Unknown/deleted ref → GraphQL error
  `code: ITEM_NOT_FOUND`.
- **No partial data:** if any aliased item in a single query is not found, the
  server returns `data: null` for the WHOLE response. Therefore fetch **one item
  per request** with bounded concurrency, tolerating not-found per item.
- `Item` fields used: `id, key, name, type, completion,
  lastCatalogMetadataUpdate, catalogCode, lifecycleStage, shared, orphan,
  descriptionV2 { content { content, contentType }, summary, lifecycle }`.
  - **Description type** = `descriptionV2.content.contentType` (enum
    `RAW | HTML`). `lifecycle` = `SYNCED_WITH_SOURCE | USER_DEFINED`.
- **Owner** = `connection(ref: "curators")` (a Relay connection;
  `ConnectionReference` is a scalar). The first curator edge node is a
  **contact**: `{ id (UUID), name, key (the EMAIL), type:"contact" }`. So
  `owner_id = node.id`, `owner_name = node.name`, `owner_email = node.key`.
  (Contacts use `connection(ref: "contacts")` analogously.)
- Connections use Relay cursor pagination: page with `first` + `after`, continue
  while `pageInfo.hasNextPage`, using `pageInfo.endCursor` (not per-edge cursor).
  `items(type: ItemType!, ...)` requires a non-null type; `glossary(...)` is the
  typeless shortcut. (These list queries are available but the item enrichment
  uses single-item fetch.)

---

## 6. Collectors

Each collector calls its client, maps responses to models, and upserts in a
transaction. Run order each cycle: **users → audit → items**.

### 6.1 Users (`collectors/users.py`)
1. `export_all_users()` → CSV rows.
2. `list_permission_sets()` → `id -> permission set` lookup.
3. `iter_scim_users()` → `id -> {groups, active}`.
4. For each export row, upsert `users` on conflict `id`:
   - `id` ← `Id`; `username` and `email` ← `Email`;
     `display_name` ← `First name` + `Last name`.
   - `is_steward` ← **`License type == "Steward"`** (case-insensitive). This is
     the confirmed steward rule.
   - `roles` ← the resolved permission set (from `listPermissionSets` by
     `Permission set id`; fallback to the CSV's permission-set columns).
   - `attributes` ← export activity columns (`Phone`, documentation
     permissions/scopes, `License type`, `Creation date`, `Last login`,
     `Logins count`) + SCIM `scimGroups` and `scimActive`.
   - `first_seen_at` set once; `last_updated_at` each run.

### 6.2 Audit (`collectors/audit.py`)
- Build `id -> username` map from the `users` table.
- Iterate `Item` events; upsert `audit_events` on conflict `event_id`
  (idempotent — duplicates never create a second row):
  - `event_id` ← `id`; `occurred_at` ← `timestamp` (parse ISO with Z /
    nanoseconds); `action` ← `itemEventType`; `item_id` ← `itemId`;
    `item_type` ← `eventType` (constant `"Item"`); `item_name` ← `itemName`;
    `raw_payload` ← whole event; `collected_at` ← now.
  - `user_id` ← `origin.id` only when `origin.originType == "User"` AND that id
    exists in `users` (else null, to avoid FK violation); `username` backfilled
    from the users map.

### 6.3 Items (`collectors/items.py`)
- Collect every distinct non-null `item_id` from `audit_events`.
- **Incremental + refresh-stale:** fetch items not yet stored, plus stored items
  whose `attributes.fetchedAt` is older than `REFRESH_STALE_DAYS` (default 7).
  (`last_updated_at` holds the item's catalog update time, so a separate
  `fetchedAt` stamp drives re-fetch decisions.)
- `client.fetch_items(refs)` → one request per ref, concurrency-bounded,
  skipping `ITEM_NOT_FOUND` (deleted items).
- Upsert `items` on conflict `id`:
  - `id` ← node.id; `key` ← key; `item_type` ← type; `name` ← name;
    `description` ← `descriptionV2.summary` (fallback `content.content`);
    `description_type` ← `content.contentType`;
    `owner_id/owner_name/owner_email` ← first `curators` node (id/name/key);
    `last_updated_at` ← `lastCatalogMetadataUpdate`;
    `attributes` ← `{completion, lifecycleStage, shared, catalogCode, orphan,
    descriptionLifecycle, fetchedAt}`; `first_seen_at` set once.

---

## 7. Scheduler & entrypoint

- `scheduler.py`:
  - `execute_collection(session_factory, *, users_client, audit_client,
    catalog_client) -> status`: run the three collectors, each in its own
    `session_scope`; catch per-collector exceptions (log + continue). Always
    write a `collection_runs` row with counts. Status = `success` (no errors),
    `failed` (all failed), else `partial`.
  - `run_collection(settings, session_factory, lock=None)`: build clients from
    settings; if a `lock` is given, hold it for the whole cycle.
  - `build_scheduler(settings, session_factory, lock)`: `AsyncIOScheduler` with a
    cron job from `COLLECT_CRON` (`CronTrigger.from_crontab`), `coalesce=True`,
    `max_instances=1`.
- `main.py`:
  - `_serve(settings)`: create engine; `create_schema(engine)` (build tables
    from ORM metadata); create session factory + a shared `asyncio.Lock`; build
    scheduler; add an **immediate one-off job** (runs once at startup so the DB
    is not empty); start scheduler; run the FastAPI app via
    `uvicorn.Server(...).serve()` on `WEBUI_HOST:WEBUI_PORT`, which blocks.
  - `main()`: load settings → configure logging → `asyncio.run(_serve)`.
- `database.create_schema(engine)` runs `Base.metadata.create_all` (idempotent).

---

## 8. Web UI (`webui.py`, FastAPI)

`create_app(settings, session_factory, lock)` returns a FastAPI app:
- `GET /` → an HTML page: a **“Run collection now”** button, an auto-refreshing
  table of the last 20 `collection_runs`, and a **per-service log viewer**
  (service picker + tail).
- `POST /api/collect` → if `lock.locked()` return **409** (`already running`);
  else launch `run_collection(settings, session_factory, lock)` as a background
  task and return **202**. The shared lock guarantees manual and cron runs never
  overlap.
- `GET /api/runs` → last 20 runs as JSON.
- `GET /api/logs/services` → services that have a log file in `LOG_DIR`.
- `GET /api/logs/{service}?lines=N` → tail of that service's log (plain text;
  404 if unknown). A fixed service→filename map prevents path traversal.

**Per-service logging:** every service writes a log file into the shared
`LOG_DIR` (bind-mounted to `./logs`): the collector via a `RotatingFileHandler`
(`logsetup.configure_logging`), the two Postgres services via
`logging_collector` (`db.log` / `metabase-db.log`, truncated daily), and
Metabase by teeing its stdout to `metabase.log`. The host `./logs` dir must be
writable by the container users (`chmod 777 logs`).

---

## 9. Docker Compose

Four services, all env from `.env`:
1. `db` — postgres:16, named volume, healthcheck (`pg_isready`).
2. `collector` — built from `collector/Dockerfile`, `depends_on db (healthy)`,
   `restart: unless-stopped`, **publish port 8000** (web UI). Gets all
   `ACTIAN_*`, `POSTGRES_*`, `METABASE_DB_PASSWORD`, `LOG_LEVEL`, `WEBUI_PORT`.
3. `metabase` — metabase/metabase:latest, port 3000, uses `metabase-db`.
4. `metabase-db` — a second postgres:16 for Metabase's app data (separate DB),
   named volume, healthcheck.

All services bind-mount `./logs` → `LOG_DIR` so each can write its log file and
the collector can tail them. The two Postgres services run with
`logging_collector` flags; Metabase tees stdout to `metabase.log`.

Dockerfile: `python:3.12-slim`, install `requirements.txt`, copy app,
`CMD ["python","-m","app.main"]`.

---

## 10. Metabase provisioning (`scripts/setup_metabase.py`)

Idempotent. Steps: poll `/api/health`; complete the setup wizard if
`has-user-setup` is false (else log in); **remove the default example assets**
(Sample Database, the *E-commerce Insights* dashboard, the *Examples*
collection); add a postgres database connection to `actian_companion` (the `db`
service); create-or-update saved questions (cards) by name (PUT so SQL changes
propagate); build dashboards via `PUT /api/dashboard/:id` with a `dashcards`
layout (Metabase v0.62 shape). Branch on `has-user-setup` (not the setup token,
which persists after setup).

All audit-based card SQL filters `user_id IS NOT NULL` so events not linked to a
known user are excluded from the statistics.

Create at least these cards and five example dashboards:
- **Activity** — Top Contributors (30d), Top Modified Items (30d), Weekly
  Documentation Pace, Events by Action, Daily Activity (30d).
- **Users & Stewardship** — Stewards vs Non-stewards, Users by Permission Set,
  Most Active Users (by login count = `attributes->>'Logins count'`).
- **Most Active Users** — most/least active users (top-10) for rolling 7/30/365-day
  windows (activity = count of `audit_events` by known `user_id` joined to
  `users`; least-active ranked among users with ≥1 event), plus an all-users
  all-time activity table.
- **Most Updated Items** — top-10 items by modification count + a detail table.
- **Documentation Coverage per Curator** — `coverage_ratio = (managed items the
  curator has an edit event on) / (items the curator manages)`, where managed =
  `items.owner_id` is the curator and an edit = an `audit_events` row with that
  `user_id` and `item_id`.
SQL runs against `audit_events`, `users` and `items`.

---

## 11. Discovery tool (`scripts/discover_api.py`)

A CLI (`audit | scim | users | catalog`, with `--raw` and `--extra`) that reads
`.env` and runs one live probe per API, printing status, headers, and body
(GraphQL subcommands run schema introspection). It must reflect the real
contracts: audit = POST with `eventType`; scim = Bearer; GraphQL = POST
introspection.

---

## 12. Testing (pytest)

No live instance required. Use `respx` to mock HTTP and an in-memory async
SQLite engine (StaticPool) for DB tests. Cover:
- Config: required-var validation, `database_url`, bad port.
- Models: table names, columns, unique `event_id`, four tables registered.
- API clients: audit cursor pagination + 500→ApiError; SCIM **Bearer present,
  X-API-SECRET absent** + pagination; users export flow (create→RUNNING→DONE→CSV)
  + ERROR→ApiError + permission sets; catalog `fetch_items` per-item
  not-found tolerance (assert one request per ref) + non-not-found error→ApiError.
- Collectors: steward detection; users mapping + roles resolution + idempotent
  re-run preserving `first_seen_at`; audit no-duplicate upsert + username
  backfill + unknown-origin→null FK + nanosecond timestamp parse; items mapping
  (owner from curators, description_type) + incremental skip of fresh items.
- Scheduler: `execute_collection` writes a run row (success) and marks `partial`
  on a collector failure.
- Web UI: `GET /` and `GET /api/runs`; `POST /api/collect` returns 202.

All code passes `ruff check` with no errors.

---

## 13. README & secrets

README must cover: prerequisites; quick start (`cp .env.example .env` → fill 4
required → `docker compose up -d` → wait → `pip install httpx python-dotenv` →
`python scripts/setup_metabase.py`); config reference; Metabase access
(localhost:3000) and the dashboards; collector logs; **manual collection via the
web UI (localhost:8000)**; known limitations (deleted catalog items can't be
enriched and are re-checked each run; audit Item-only; per-API auth differs).

`.gitignore` must exclude `.env` (commit only `.env.example`), `.venv/`,
`__pycache__/`, caches. Never commit real credentials.

---

## 14. Acceptance criteria

- `docker compose config` valid; `docker compose build collector` succeeds.
- `pytest` all green; `ruff check` zero errors.
- On `docker compose up -d`, the collector auto-migrates, runs once immediately,
  then on cron. A `collection_runs` row is written every run.
- Against a real instance: users populated; audit_events populated with no
  duplicate `event_id`; items enriched (id, key, name, type, description +
  type, owner id/name/email where a curator exists, catalog last-updated);
  steward flag from License type.
- Web UI at :8000 triggers a run (202) and rejects overlap (409).
- `setup_metabase.py` is idempotent and builds the two dashboards.
