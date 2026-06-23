"""Shared HTTP plumbing for the API clients.

Centralises the three things every client needs so each client stays thin and
consistent:

* auth headers -- `X-API-SECRET` for Audit / Catalog / User Management,
  `Authorization: Bearer` for SCIM;
* error handling -- any HTTP/transport failure is logged and re-raised as a
  single `ApiError`, so a caller (a collector) can contain one API's failure;
* a GraphQL POST helper that unwraps the `{ "data", "errors" }` envelope and can
  tolerate an expected error code (e.g. Catalog `ITEM_NOT_FOUND`).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.api import ApiError

logger = logging.getLogger(__name__)


def secret_headers(api_key: str, *, json_body: bool = False) -> dict[str, str]:
    """Auth headers for the X-API-SECRET APIs (Audit, Catalog, User Management)."""
    headers = {"X-API-SECRET": api_key, "Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def bearer_headers(api_key: str) -> dict[str, str]:
    """Auth headers for the SCIM API (Bearer token, SCIM JSON)."""
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/scim+json"}


def make_client(
    base_url: str, headers: dict[str, str], timeout: float
) -> httpx.AsyncClient:
    """Create an async HTTP client bound to the instance base URL and headers."""
    return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)


def secret_client(base_url: str, api_key: str, timeout: float) -> httpx.AsyncClient:
    """Async client for the X-API-SECRET JSON APIs (Catalog, User Management)."""
    return make_client(base_url, secret_headers(api_key, json_body=True), timeout)


async def send_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    label: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Send a request and return parsed JSON; raise `ApiError` (logged) on failure.

    `label` names the API in log messages and the raised error.
    """
    try:
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "%s returned HTTP %s: %s", label, exc.response.status_code, exc.response.text
        )
        raise ApiError(f"{label} request failed") from exc
    except httpx.HTTPError as exc:
        logger.error("%s request error: %s", label, exc)
        raise ApiError(f"{label} request failed") from exc
    return response.json()


async def send_text(url: str, *, label: str, timeout: float) -> str:
    """GET an absolute, unauthenticated URL and return its body text.

    Used for the presigned S3 link returned by the user export.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error("%s HTTP %s: %s", label, exc.response.status_code, exc.response.text)
        raise ApiError(f"{label} failed") from exc
    except httpx.HTTPError as exc:
        logger.error("%s error: %s", label, exc)
        raise ApiError(f"{label} failed") from exc
    return response.text


async def graphql(
    client: httpx.AsyncClient,
    path: str,
    query: str,
    *,
    label: str,
    variables: dict[str, Any] | None = None,
    tolerated_error_code: str | None = None,
) -> dict[str, Any]:
    """POST a GraphQL operation and return its `data`.

    Raises `ApiError` on transport/HTTP failure or GraphQL `errors` -- unless
    every error carries `tolerated_error_code` (e.g. `ITEM_NOT_FOUND`), in which
    case the partial `data` is returned.
    """
    payload: dict[str, Any] = {"query": query}
    if variables is not None:
        payload["variables"] = variables

    body = await send_json(client, "POST", path, label=label, json=payload)

    errors = body.get("errors")
    if errors:
        all_tolerated = tolerated_error_code is not None and all(
            (e.get("extensions") or {}).get("code") == tolerated_error_code
            for e in errors
        )
        if all_tolerated:
            logger.info("%s: %s %s error(s) tolerated", label, len(errors), tolerated_error_code)
        else:
            logger.error("%s GraphQL errors: %s", label, errors)
            raise ApiError(f"{label} returned GraphQL errors")
    return body.get("data") or {}
