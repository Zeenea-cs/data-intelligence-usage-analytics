# Actian (Zeenea) API Discovery

Source of truth for API client implementation. Produced via the discovery
protocol in `CLAUDE.md` against a live preprod instance.

- **Instance:** `https://customer-services.preprod.zeenea.app`
- **Auth:** header `X-API-SECRET: {API_KEY}` on every request (confirmed; not
  `Authorization`).
- **Platform:** Zeenea / Actian Data Intelligence. Errors use RFC-7807 shape:
  `{ "type", "title", "status", "detail" }`.

---

## 1. Audit API — REST — STATUS: ✅ confirmed

- **Method/endpoint:** `POST /public-api/management/audit`
  (the base path is **POST**, not GET — a bare `GET` returns 404
  `entity-not-found`).
- **Headers:** `X-API-SECRET`, `Content-Type: application/json`.

### Request body
| Field | Required | Notes |
|---|---|---|
| `eventType` | **yes** | Enum, case-sensitive. Valid: `Item`, `User`, `Group`, `ApiKey`, `PermissionSet`. Invalid value → 500 `No value found: X`. |
| `from` | **yes** | ISO-8601, e.g. `2026-06-12T00:00:00.000Z`. Window start. Omitting it → 500 `obj.from error.path.missing` (confirmed against the live instance — both bounds are mandatory). |
| `to` | **yes** | ISO-8601. Window end. Omitting it → 500 `obj.to error.path.missing`. |
| `cursorMark` | no | Pagination cursor; echo back the value from the previous response. |

> `pageSize` is **ignored** by this API (returned the full page regardless).

### Pagination — cursor-based
- Response includes `pagination.cursorMark`.
- To page: resend the same body with `cursorMark` set to the previous response's
  value.
- **Stop** when `data` is empty or the returned `cursorMark` is `""`.
- Do **not** assume `page`/`pageSize` here (that pattern does not apply).

### Response envelope
```json
{ "data": [ /* events */ ], "pagination": { "cursorMark": "<ts>|<uuid>" } }
```

### `Item` event shape (the type we collect)
```json
{
  "id": "24bb9978-45b1-4b94-a19c-649c71794657",
  "timestamp": "2026-06-15T15:30:10.976Z",
  "eventType": "Item",
  "origin": { "id": "55b1e042-...", "originType": "User" },
  "value": { "Short Text Test": ["..."] },
  "previousValue": { "...": "..." },        // present on updates only
  "itemId": "9a3fb916-433d-4a49-8f9e-05373692b756",
  "itemName": "( ICPE ) Carrières autorisées en Pays de la Loire",
  "itemEventType": "CreateItem"             // or UpdateItem / DeleteItem
}
```
Notes:
- `Item` events carry **no username** and **no item-type** field. `origin` gives
  only the actor `id` + `originType`.
- Other event types have different fields and are **out of scope** for phase 1:
  - `User`: `userId`, `userEmail`, `userName`, `userEventType`
  - `ApiKey`: `apiKeyId`, `apiKeyName`, `apiKeyEventType`
  - `Group`/`PermissionSet`: `previousValue`/`value` = `{id,name,codeKey}`

### Decisions (confirmed by human)
- **Scope:** collect `eventType=Item` only. (Matches item-centric `audit_events`
  and all three Metabase questions.)
- **username:** backfill via lookup on `users.id = origin.id` (users collected
  first each run); null if user unknown.
- **item_type:** store `eventType` (always `"Item"`); real per-item type comes
  from the Catalog/items table in phase 2.

### Field mapping → `audit_events`
| API field | DB column | Note |
|---|---|---|
| `id` | `event_id` | UNIQUE; upsert key (`ON CONFLICT (event_id)`) |
| `origin.id` | `user_id` | FK → `users.id`, only when `origin.originType == "User"` |
| _(lookup)_ | `username` | from `users` by `user_id`; null if unknown |
| `itemEventType` | `action` | `CreateItem` / `UpdateItem` / `DeleteItem` |
| `itemId` | `item_id` | |
| `eventType` | `item_type` | constant `"Item"` for collected rows |
| `itemName` | `item_name` | |
| `timestamp` | `occurred_at` | TIMESTAMPTZ |
| _(whole event JSON)_ | `raw_payload` | JSONB |
| _(now, set at insert)_ | `collected_at` | TIMESTAMPTZ |

### Discovery command
```
python scripts/discover_api.py audit                 # default eventType=Item
python scripts/discover_api.py --extra User audit     # other event type
python scripts/discover_api.py --raw audit            # full untruncated body
```

---

## 2. SCIM API — REST — STATUS: ✅ confirmed

- **Endpoint:** `GET /api/scim/v2/Users` (the base `/api/scim/v2` is not listable;
  use the `/Users` collection).
- **Auth — DEVIATION:** SCIM uses **`Authorization: Bearer {key}`**, NOT
  `X-API-SECRET`. Sending `X-API-SECRET` → `401 Invalid bearer`. The other three
  APIs keep `X-API-SECRET`. (Confirmed with human: Bearer for SCIM only.)
- **Accept:** `application/scim+json`.
- Other working endpoints: `/ServiceProviderConfig`, `/ResourceTypes`,
  `/Schemas` (all GET, Bearer).

### Pagination — SCIM standard (1-based offset)
| Param | Notes |
|---|---|
| `startIndex` | 1-based index of first result. |
| `count` | page size. |

Response envelope reports `totalResults`, `itemsPerPage`, `startIndex`. Page
until `startIndex + itemsPerPage > totalResults`. (Instance currently:
`totalResults = 38`.)

### Response envelope
```json
{
  "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
  "totalResults": 38, "itemsPerPage": 38, "startIndex": 1,
  "Resources": [ /* users */ ]
}
```

### User resource shape
```json
{
  "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
  "id": "47f56e77-c052-4e9f-8ed0-bdf3f255d720",
  "userName": "ahamza@zeenea.com",
  "name": { "familyName": "Hamza", "givenName": "Amir" },
  "active": true,
  "emails": [ { "value": "ahamza@zeenea.com" } ],
  "groups": [ { "value": "b5df8dee-...", "display": "Super Admin" } ]
}
```

### Roles & steward (confirmed by human)
- There is **no `roles` field**; roles are expressed as **`groups[]`**, each
  `{ value (id), display (name) }`.
- Distinct group displays observed: `Data Steward` (22 users), `Super Admin`
  (11), `Explorer`, `Reports only`, `Explorer A`, `catalog_a_ds`, `Empty Group`,
  `Metamodel Admin Only`, `Scim Aware`.
- **Steward rule:** `is_steward = True` if any `groups[].display` matches
  `steward` **case-insensitively** (regex `/steward/i`). Matches `Data Steward`.

### Field mapping → `users` (SCIM-sourced columns)
| API field | DB column | Note |
|---|---|---|
| `id` | `id` | PK; upsert key (`ON CONFLICT (id)`) |
| `userName` | `username` | |
| `emails[0].value` | `email` | falls back to `userName` if absent |
| `name.givenName` + `name.familyName` | `display_name` | joined `"Given Family"` |
| `groups` | `roles` | raw groups array stored as JSONB |
| _(derived)_ | `is_steward` | from steward rule above |
| `active`, `name`, `schemas` (remainder) | `attributes` | additional SCIM attrs as JSONB |
| _(now, first insert)_ | `first_seen_at` | set once |
| _(now, each upsert)_ | `last_updated_at` | |

> Reconciliation with the User Management GraphQL API (#3) is decided after #3 is
> discovered — see that section for which source is authoritative per column.

### Discovery command
```
python scripts/discover_api.py scim                       # GET /Users count=1
python scripts/discover_api.py --extra /api/scim/v2/Schemas scim
python scripts/discover_api.py --raw scim
```

---

## 3. User Management API — GraphQL — STATUS: ✅ confirmed

- **Endpoint:** `POST /public-api/catalog/graphql` (GraphQL; body `{"query","variables"}`).
- **Auth:** `X-API-SECRET`.
- **SCOPE (confirmed by human): only three GraphQL operations are used —**
  `createAllUsersExport` (mutation), `loadUsersExportStatus` (query), and
  `listPermissionSets` (query). The first produces the `exportId` that feeds the
  second; the third resolves the permission set referenced in the export CSV.
  All other operations (`loadUserById`, `loadUserByEmail`, `listItemTypes`, …)
  are **out of scope**.

### Bulk user export — the only User Management flow used
Three-step async flow:
1. **Mutation** `createAllUsersExport { exportId }` → returns an `exportId` (UUID).
2. **Query** `loadUsersExportStatus(input:{exportId}) { exportId status url }` —
   pass the `exportId` from step 1. Poll until `status == DONE`. Enum
   `UsersExportStatus = RUNNING | DONE | ERROR` (no PENDING). On `DONE`, `url` is
   a **presigned S3 URL** (expires ~1h).
3. **GET** the `url` (no auth header needed; presigned). Returns **CSV**
   (`text/csv`).

#### Export CSV columns (header row, exact)
```
Id, Email, First name, Last name, Phone,
Permission set id, Permission set name, Permission set description, Permission set built-in,
Custom item documentation permission, Custom item documentation scope,
Item documentation permission, Item documentation scope,
Glossary documentation permission, Glossary documentation scope,
Catalog design permission, Ops administration permission,
Users and permissions administration permission, Analytics dashboard permission,
License type, Creation date, Last login, Logins count
```
`License type` ∈ `LicenseType` enum = `Explorer | Steward`.

### `listPermissionSets` — resolve the export's permission set
```
listPermissionSets { id name description builtIn licenseType permissions { permission } }
```
Returns the full catalog of permission sets (14 on this instance; many
`licenseType=Steward`, incl. `Data Steward`, `steward C`, `Steward Agents`,
`Super Admin`). Use it to resolve the export CSV's **`Permission set id`** →
`{ name, description, builtIn, licenseType, permissions[] }`. Call once per run
and build an `id → permission set` lookup; join each exported user's
`Permission set id` against it to populate `users.roles`.

### Decisions (confirmed by human)
- **Steward rule (final):** `is_steward = (License type == "Steward")` from the
  export. (Supersedes the SCIM-group rule. 32/38 users on this instance.)
- **User source:** the export CSV (enumerate + steward), one call per run.
- **roles column:** the export's `Permission set id` resolved via
  `listPermissionSets` to the full permission set (name, description, builtIn,
  licenseType, permissions[]), stored as JSON.
- **SCIM enrichment:** also call SCIM `/Users` and attach `groups[]` (+ `active`)
  into `attributes`.

---

## CONSOLIDATED user-collection plan & `users` mapping

Per run, the users collector:
1. `createAllUsersExport` → poll `loadUsersExportStatus` until `DONE` → GET CSV.
2. `listPermissionSets` → build an `id → permission set` lookup.
3. SCIM `GET /api/scim/v2/Users` (Bearer) for `groups[]` + `active`, keyed by id.
4. For each exported user: resolve `Permission set id` via the lookup; upsert
   into `users` (`ON CONFLICT (id) DO UPDATE`).

| `users` column | Source | Note |
|---|---|---|
| `id` | export `Id` | PK / upsert key (matches SCIM `id`) |
| `username` | export `Email` | (instance has no separate username) |
| `email` | export `Email` | |
| `display_name` | export `First name` + `Last name` | |
| `is_steward` | export `License type == "Steward"` | final steward rule |
| `roles` | export `Permission set id` → `listPermissionSets` | resolved permission set (name, description, builtIn, licenseType, permissions[]) → JSONB |
| `attributes` | export activity + SCIM | `Creation date`, `Last login`, `Logins count`, scopes, SCIM `groups[]`, `active` → JSONB |
| `first_seen_at` | collector | set on first insert |
| `last_updated_at` | collector | set every upsert |

### Discovery command
```
python scripts/discover_api.py users     # GraphQL introspection (user-defined types)
```
(Export flow is exercised by the collector, not the discovery probe.)

---

## 4. Catalog API — GraphQL — STATUS: ✅ implemented (items enrichment live)

- **Endpoint:** `POST /api/catalog/graphql` (GraphQL). Auth: `X-API-SECRET`.
  Introspection enabled.
- **Query root (item-relevant):**
  - `item(ref: ItemReference!) -> Item` — fetch one item by **UUID or key**.
    `ItemReference` is a SCALAR. Unknown/deleted refs → GraphQL error
    `code: ITEM_NOT_FOUND`.
  - `items(type: ItemType!, first, after) -> ItemConnection` — `type` required.
  - `glossary(first, after) -> ItemConnection` — typeless shortcut.
  - `itemByName(name, type)`, `node(id)`, `datasource(ref)`.
- **`ItemType` is a dynamic SCALAR** (metamodel keys, e.g. `businessconcept`,
  `entity`, `dataset`).
- **No partial data:** if any aliased item in a multi-item query is
  ITEM_NOT_FOUND, the server returns `data: null` for the WHOLE response. So the
  collector fetches **one item per request** (bounded concurrency), tolerating
  not-found per item, rather than aliased batches.

### Owner / curators (confirmed by human + probe)
- Owner = the **`connection(ref: "curators")`** connection; contacts =
  `connection(ref: "contacts")`. `ConnectionReference` is a SCALAR.
- A curator edge node is a **contact-type item**:
  `{ id (UUID), name ("David Martin"), key (the EMAIL), type ("contact") }`.
  So owner email = curator node `key`.

### Description (confirmed by probe)
- `descriptionV2 { content { content, contentType }, summary, lifecycle }`.
- `content.contentType` is the **description type** enum = `RAW | HTML`.
- `lifecycle` = `SYNCED_WITH_SOURCE | USER_DEFINED` (stored in attributes).

### Pagination — Relay cursor connections
`ItemConnection { nodes, edges { cursor, node }, pageInfo, totalCount }`,
`PageInfo { startCursor, endCursor, hasNextPage, hasPreviousPage }`.
- Page forward with `first: N, after: <endCursor>`; continue while
  `pageInfo.hasNextPage`.
- **Use `pageInfo.endCursor`**, not per-edge `cursor` (some edges return
  `"No cursor available for this Item"`).

### `Item` interface shape
```
Item { id: ID, type: ItemType, key: String, name: String,
       completion: Percentage, descriptionV2: ItemDescription,
       lastCatalogMetadataUpdate: DateTime, property: PropertyValue,
       deletionDate: DateTime, orphan: Boolean, connection: ItemConnection,
       catalogCode: String, lifecycleStage: String, shared: Boolean }
ItemDescription { content: Text, summary: LongText, lifecycle: DescriptionLifecycle }
```
Implementing object types: `Dataset`, `Field`, `GenericItem`, `SemanticItem`
(all expose the `Item` interface fields).

### Field mapping → `items` (implemented; migration 002 added columns)
| `items` column | Catalog field | Note |
|---|---|---|
| `id` | `id` | PK / upsert key (the audit `item_id` UUID) |
| `key` | `key` | |
| `item_type` | `type` | scalar type key (e.g. `businessconcept`) |
| `name` | `name` | |
| `description` | `descriptionV2.summary` (fallback `content.content`) | TEXT |
| `description_type` | `descriptionV2.content.contentType` | `RAW` / `HTML` |
| `owner_id` | curators[0].node.`id` | first curator |
| `owner_name` | curators[0].node.`name` | |
| `owner_email` | curators[0].node.`key` | curator is a contact; key = email |
| `attributes` | `completion`, `lifecycleStage`, `shared`, `catalogCode`, `orphan`, `descriptionV2.lifecycle`, plus `fetchedAt` | JSONB |
| `first_seen_at` | collector | set once |
| `last_updated_at` | `lastCatalogMetadataUpdate` | the item's catalog update time (user-facing) |

> **Collection strategy:** for every distinct `item_id` in `audit_events`, fetch
> from the Catalog (incremental + refresh-stale: new items + those whose
> `attributes.fetchedAt` is older than `REFRESH_STALE_DAYS=7`). Deleted items
> (ITEM_NOT_FOUND) are skipped. `collectors/items.py` runs in the scheduled cycle
> after audit.

### Discovery command
```
python scripts/discover_api.py catalog    # GraphQL introspection
```

---

## Summary of cross-API deviations from CLAUDE.md
1. **SCIM auth** uses `Authorization: Bearer {key}`, not `X-API-SECRET`.
2. **Audit** is `POST` (not GET) and requires an `eventType` enum; pagination is
   `cursorMark` (cursor), not `page`/`pageSize` — `pageSize` is ignored.
3. **User enumeration** is via an async CSV export
   (`createAllUsersExport` → `loadUsersExportStatus` → presigned S3 URL), since
   the GraphQL API has no list-users query.
4. **Steward** = `License type == "Steward"` (from the export), not a roles-array
   string match.
5. **Catalog** pagination is Relay cursor (`first`/`after`/`endCursor`), and
   `items(type:)` requires a non-null type.
6. **Catalog item owner** has no direct field — it is the `connection(ref:
   "curators")` (a contact whose `key` is the email). And multi-item queries
   return `data: null` wholesale on any ITEM_NOT_FOUND, so items are fetched one
   request at a time.
