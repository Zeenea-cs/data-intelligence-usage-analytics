"""Interactive API discovery tool.

Run BEFORE implementing any API client (see the discovery protocol in CLAUDE.md).
Reads ACTIAN_INSTANCE_URL and ACTIAN_API_KEY from .env and probes one API at a
time, printing raw JSON, HTTP status, and response headers.

Subcommands:
    audit    -- GET the Audit REST endpoint (then pageSize=1 if supported).
    scim     -- GET the SCIM REST endpoint (fallback: /api/scim/v2/Users).
    users    -- GraphQL introspection against the User Management API.
    catalog  -- GraphQL introspection against the Catalog API.

Flags:
    --raw    -- dump the full unfiltered response.
    --extra  -- per-subcommand extra: a path suffix (REST) or a query string
                (GraphQL) to run a targeted probe instead of the default.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from dotenv import load_dotenv

AUDIT_PATH = "/public-api/management/audit"
SCIM_PATH = "/api/scim/v2"
CATALOG_GRAPHQL_PATH = "/api/catalog/graphql"
USERS_GRAPHQL_PATH = "/public-api/catalog/graphql"

INTROSPECTION_QUERY = (
    "{ __schema { types { name kind fields { name type { name kind "
    "ofType { name kind } } } } } }"
)

# GraphQL scalar type names that carry no useful schema detail for discovery.
_BUILTIN_SCALARS = {"String", "Int", "Float", "Boolean", "ID"}


def _credentials() -> tuple[str, str]:
    """Return (instance_url, api_key) from the environment, stripping any slash."""
    load_dotenv()
    url = os.environ.get("ACTIAN_INSTANCE_URL")
    key = os.environ.get("ACTIAN_API_KEY")
    if not url or not key:
        sys.exit("ACTIAN_INSTANCE_URL and ACTIAN_API_KEY must be set (see .env).")
    return url.rstrip("/"), key


def _headers(api_key: str) -> dict[str, str]:
    """Build the auth headers used on every request."""
    return {"X-API-SECRET": api_key, "Accept": "application/json"}


def _report(label: str, response: httpx.Response, *, raw: bool) -> None:
    """Print status, headers, and body for one probe response."""
    print(f"\n=== {label} ===")
    print(f"HTTP {response.status_code} {response.reason_phrase}")
    print(f"URL: {response.request.method} {response.request.url}")
    print("--- response headers ---")
    for name, value in response.headers.items():
        print(f"{name}: {value}")
    print("--- body ---")
    try:
        body = response.json()
        if not raw:
            body = _truncate(body)
        print(json.dumps(body, indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print(response.text)


def _truncate(body: object, *, limit: int = 5) -> object:
    """Trim long lists in a JSON body so default output stays readable."""
    if isinstance(body, list):
        trimmed = [_truncate(item) for item in body[:limit]]
        if len(body) > limit:
            trimmed.append(f"... ({len(body) - limit} more items omitted)")
        return trimmed
    if isinstance(body, dict):
        return {key: _truncate(value) for key, value in body.items()}
    return body


def _audit_probe(*, raw: bool, extra: str | None) -> None:
    """Probe the Audit API (POST). Body requires `eventType`; paginates via cursorMark.

    `extra`, if given, overrides the eventType (default: Item).
    """
    url, key = _credentials()
    event_type = extra or "Item"
    headers = {**_headers(key), "Content-Type": "application/json"}
    body = {
        "from": "2026-06-12T00:00:00.000Z",
        "to": "2026-06-19T00:00:00.000Z",
        "eventType": event_type,
    }
    with httpx.Client(base_url=url, headers=headers, timeout=30.0) as client:
        response = client.post(AUDIT_PATH, json=body)
    _report(f"Audit API (POST eventType={event_type})", response, raw=raw)


def _scim_probe(*, raw: bool, extra: str | None) -> None:
    """Probe the SCIM API.

    SCIM authenticates with ``Authorization: Bearer {key}`` (NOT X-API-SECRET)
    and paginates with ``startIndex``/``count``. Defaults to the /Users
    collection; ``extra`` overrides the path suffix.
    """
    url, key = _credentials()
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/scim+json",
    }
    path = extra or f"{SCIM_PATH}/Users"
    with httpx.Client(base_url=url, headers=headers, timeout=30.0) as client:
        _report(
            f"SCIM API {path} (count=1)",
            client.get(path, params={"count": 1}),
            raw=raw,
        )


def _graphql_probe(path: str, label: str, *, raw: bool, extra: str | None) -> None:
    """Run a GraphQL probe: a custom query if given, else schema introspection."""
    url, key = _credentials()
    query = extra or INTROSPECTION_QUERY
    with httpx.Client(base_url=url, headers=_headers(key), timeout=30.0) as client:
        response = client.post(
            path,
            json={"query": query},
            headers={"Content-Type": "application/json"},
        )
    if extra or raw:
        _report(f"{label} (query)", response, raw=raw)
        return
    _report_introspection(label, response)


def _report_introspection(label: str, response: httpx.Response) -> None:
    """Print introspection output filtered to user-defined types only."""
    print(f"\n=== {label} (introspection) ===")
    print(f"HTTP {response.status_code} {response.reason_phrase}")
    try:
        payload = response.json()
    except json.JSONDecodeError:
        print(response.text)
        return
    if payload.get("errors"):
        print("GraphQL errors (introspection may be disabled):")
        print(json.dumps(payload["errors"], indent=2, ensure_ascii=False))
        return
    types = payload.get("data", {}).get("__schema", {}).get("types", [])
    kept = [
        t
        for t in types
        if not t["name"].startswith("__") and t["name"] not in _BUILTIN_SCALARS
    ]
    print(f"User-defined types: {len(kept)}")
    print(json.dumps(kept, indent=2, ensure_ascii=False))


def main() -> None:
    """Parse CLI subcommands and dispatch to the matching probe."""
    parser = argparse.ArgumentParser(description="Actian API discovery tool.")
    parser.add_argument("--raw", action="store_true", help="dump full response")
    parser.add_argument(
        "--extra",
        default=None,
        help="REST: path suffix to GET; GraphQL: query string to POST",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "scim", "users", "catalog"):
        sub.add_parser(name)
    args = parser.parse_args()

    if args.command == "audit":
        _audit_probe(raw=args.raw, extra=args.extra)
    elif args.command == "scim":
        _scim_probe(raw=args.raw, extra=args.extra)
    elif args.command == "users":
        _graphql_probe(
            USERS_GRAPHQL_PATH, "User Management API", raw=args.raw, extra=args.extra
        )
    elif args.command == "catalog":
        _graphql_probe(
            CATALOG_GRAPHQL_PATH, "Catalog API", raw=args.raw, extra=args.extra
        )


if __name__ == "__main__":
    main()
