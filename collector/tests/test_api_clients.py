"""Unit tests for the API clients using respx mocks.

Payloads are copied from real responses observed during discovery (DISCOVERY.md)
and trimmed. No live Actian instance is required.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from app.api import ApiError
from app.api.audit import AuditClient
from app.api.catalog import CatalogClient
from app.api.users import UsersClient

BASE = "https://test.example"


async def _drain(aiter: AsyncIterator[Any]) -> list[Any]:
    """Collect an async iterator into a list."""
    return [item async for item in aiter]


# --------------------------------------------------------------------------- #
# Audit API
# --------------------------------------------------------------------------- #

_AUDIT_PAGE1 = {
    "data": [
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "timestamp": "2026-06-15T15:30:10.976Z",
            "eventType": "Item",
            "origin": {"id": "22222222-2222-4222-8222-222222222222", "originType": "User"},
            "value": {"Sample Text Property": ["x"]},
            "itemId": "33333333-3333-4333-8333-333333333333",
            "itemName": "Sample Dataset",
            "itemEventType": "CreateItem",
        },
        {
            "id": "44444444-4444-4444-8444-444444444444",
            "timestamp": "2026-06-15T15:30:37.250Z",
            "eventType": "Item",
            "origin": {"id": "22222222-2222-4222-8222-222222222222", "originType": "User"},
            "previousValue": {"Sample Text Property": ["x"]},
            "value": {"Sample Text Property": ["x", "hello world"]},
            "itemId": "33333333-3333-4333-8333-333333333333",
            "itemName": "Sample Dataset",
            "itemEventType": "UpdateItem",
        },
    ],
    "pagination": {"cursorMark": "2026-06-17T10:30:29.228Z|53b5eceb"},
}
_AUDIT_PAGE2_EMPTY = {"data": [], "pagination": {"cursorMark": ""}}


@respx.mock
def test_audit_paginates_via_cursormark() -> None:
    """iter_item_events follows cursorMark and stops on the empty page."""
    route = respx.post(f"{BASE}/public-api/management/audit").mock(
        side_effect=[
            httpx.Response(200, json=_AUDIT_PAGE1),
            httpx.Response(200, json=_AUDIT_PAGE2_EMPTY),
        ]
    )
    client = AuditClient(BASE, "secret-key")
    events = asyncio.run(_drain(client.iter_item_events(since="2026-06-12T00:00:00.000Z")))

    assert [e["id"] for e in events] == [
        "11111111-1111-4111-8111-111111111111",
        "44444444-4444-4444-8444-444444444444",
    ]
    assert route.call_count == 2
    # First request: required eventType, no cursor. Auth header present.
    first = route.calls[0].request
    assert first.headers["x-api-secret"] == "secret-key"
    import json

    assert json.loads(first.content)["eventType"] == "Item"
    assert "cursorMark" not in json.loads(first.content)
    # Second request echoes the cursor from page 1.
    assert json.loads(route.calls[1].request.content)["cursorMark"] == (
        "2026-06-17T10:30:29.228Z|53b5eceb"
    )


@respx.mock
def test_audit_raises_apierror_on_http_500() -> None:
    """A 500 is logged and surfaced as ApiError."""
    respx.post(f"{BASE}/public-api/management/audit").mock(
        return_value=httpx.Response(500, json={"status": 500, "title": "boom"})
    )
    client = AuditClient(BASE, "k")
    with pytest.raises(ApiError):
        asyncio.run(_drain(client.iter_item_events()))


# --------------------------------------------------------------------------- #
# Catalog API
# --------------------------------------------------------------------------- #


def _catalog_page(nodes: list[dict[str, Any]], *, has_next: bool, cursor: str) -> dict:
    return {
        "data": {
            "items": {
                "totalCount": 3,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "edges": [{"cursor": "*", "node": n} for n in nodes],
            }
        }
    }


_ITEM_A = {"id": "0d1695ac", "type": "businessconcept", "name": "Customer", "key": "businessconcept/Customer"}
_ITEM_B = {"id": "124ef5bd", "type": "EA1", "name": "Enterprise Data Model", "key": "EA1/EDM"}
_ITEM_C = {"id": "33333333", "type": "businessconcept", "name": "Product", "key": "businessconcept/Product"}


@respx.mock
def test_catalog_items_relay_pagination() -> None:
    """iter_items pages via pageInfo.endCursor until hasNextPage is false."""
    route = respx.post(f"{BASE}/api/catalog/graphql").mock(
        side_effect=[
            httpx.Response(200, json=_catalog_page([_ITEM_A, _ITEM_B], has_next=True, cursor="C1")),
            httpx.Response(200, json=_catalog_page([_ITEM_C], has_next=False, cursor="C2")),
        ]
    )
    client = CatalogClient(BASE, "secret-key")
    nodes = asyncio.run(_drain(client.iter_items("businessconcept", page_size=2)))

    assert [n["id"] for n in nodes] == ["0d1695ac", "124ef5bd", "33333333"]
    assert route.call_count == 2
    assert route.calls[0].request.headers["x-api-secret"] == "secret-key"
    import json

    # Page 2 sends after=<endCursor of page 1>.
    assert json.loads(route.calls[1].request.content)["variables"]["after"] == "C1"


@respx.mock
def test_catalog_graphql_errors_raise_apierror() -> None:
    """A 200 response carrying GraphQL `errors` becomes an ApiError."""
    respx.post(f"{BASE}/api/catalog/graphql").mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "bad"}]})
    )
    client = CatalogClient(BASE, "k")
    with pytest.raises(ApiError):
        asyncio.run(_drain(client.iter_items("businessconcept")))


# --------------------------------------------------------------------------- #
# User Management API — export + permission sets
# --------------------------------------------------------------------------- #

_EXPORT_CSV = (
    '"Id","Email","First name","Last name","Phone",'
    '"Permission set id","Permission set name","Permission set description",'
    '"Permission set built-in","Custom item documentation permission",'
    '"Custom item documentation scope","Item documentation permission",'
    '"Item documentation scope","Glossary documentation permission",'
    '"Glossary documentation scope","Catalog design permission",'
    '"Ops administration permission","Users and permissions administration permission",'
    '"Analytics dashboard permission","License type","Creation date","Last login",'
    '"Logins count"\r\n'
    '"22222222-2222-4222-8222-222222222222","alex.curator@example.com","Alex","Curator","",'
    '"66666666-6666-4666-8666-666666666666","Super Admin","desc","false","true","All",'
    '"true","All","true","All","true","true","true","true","Steward",'
    '"2023-12-12T10:16:37Z","2026-06-18T13:42:28Z","600"\r\n'
)

_EXPORT_URL = "https://files.example/zeenea-users.csv?sig=abc"


@respx.mock
def test_users_export_flow_runs_polls_and_parses_csv() -> None:
    """export_all_users: create -> poll RUNNING -> DONE -> download + parse CSV."""
    graphql = respx.post(f"{BASE}/public-api/catalog/graphql").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"createAllUsersExport": {"exportId": "exp-1"}}}),
            httpx.Response(200, json={"data": {"loadUsersExportStatus": {"exportId": "exp-1", "status": "RUNNING", "url": None}}}),
            httpx.Response(200, json={"data": {"loadUsersExportStatus": {"exportId": "exp-1", "status": "DONE", "url": _EXPORT_URL}}}),
        ]
    )
    download = respx.get(_EXPORT_URL).mock(
        return_value=httpx.Response(200, text=_EXPORT_CSV, headers={"content-type": "text/csv"})
    )
    client = UsersClient(BASE, "secret-key", poll_interval=0)
    rows = asyncio.run(client.export_all_users())

    assert graphql.call_count == 3
    assert download.call_count == 1
    assert graphql.calls[0].request.headers["x-api-secret"] == "secret-key"
    assert len(rows) == 1
    assert rows[0]["Email"] == "alex.curator@example.com"
    assert rows[0]["License type"] == "Steward"
    assert rows[0]["Permission set id"] == "66666666-6666-4666-8666-666666666666"


@respx.mock
def test_users_export_status_error_raises() -> None:
    """An ERROR export status raises ApiError."""
    respx.post(f"{BASE}/public-api/catalog/graphql").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"createAllUsersExport": {"exportId": "exp-1"}}}),
            httpx.Response(200, json={"data": {"loadUsersExportStatus": {"exportId": "exp-1", "status": "ERROR", "url": None}}}),
        ]
    )
    client = UsersClient(BASE, "k", poll_interval=0)
    with pytest.raises(ApiError):
        asyncio.run(client.export_all_users())


@respx.mock
def test_list_permission_sets_returns_catalog() -> None:
    """list_permission_sets returns the permission-set list."""
    respx.post(f"{BASE}/public-api/catalog/graphql").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "listPermissionSets": [
                        {"id": "66666666", "name": "Super Admin", "builtIn": True, "licenseType": "Steward", "permissions": []},
                        {"id": "cec5c187", "name": "Explorer", "builtIn": False, "licenseType": "Explorer", "permissions": []},
                    ]
                }
            },
        )
    )
    client = UsersClient(BASE, "secret-key")
    sets = asyncio.run(client.list_permission_sets())
    assert {s["name"] for s in sets} == {"Super Admin", "Explorer"}


# --------------------------------------------------------------------------- #
# SCIM API
# --------------------------------------------------------------------------- #


def _scim_user(uid: str, username: str, group: str) -> dict[str, Any]:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "id": uid,
        "userName": username,
        "name": {"familyName": "Doe", "givenName": "Jane"},
        "active": True,
        "emails": [{"value": username}],
        "groups": [{"value": "g1", "display": group}],
    }


@respx.mock
def test_scim_uses_bearer_and_paginates() -> None:
    """iter_scim_users uses Bearer auth (not X-API-SECRET) and pages startIndex/count."""
    page1 = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": 3, "itemsPerPage": 2, "startIndex": 1,
        "Resources": [_scim_user("1", "a@x.com", "Super Admin"), _scim_user("2", "b@x.com", "Data Steward")],
    }
    page2 = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": 3, "itemsPerPage": 1, "startIndex": 3,
        "Resources": [_scim_user("3", "c@x.com", "Explorer")],
    }
    route = respx.get(f"{BASE}/api/scim/v2/Users").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    client = UsersClient(BASE, "secret-key")
    users = asyncio.run(_drain(client.iter_scim_users(page_size=2)))

    assert [u["id"] for u in users] == ["1", "2", "3"]
    assert route.call_count == 2
    headers = route.calls[0].request.headers
    assert headers["authorization"] == "Bearer secret-key"
    assert "x-api-secret" not in headers
