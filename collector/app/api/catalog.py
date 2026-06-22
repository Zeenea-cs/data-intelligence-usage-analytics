"""Catalog API client (GraphQL).

Confirmed contract (see DISCOVERY.md §4):
- Endpoint: POST /api/catalog/graphql. Auth: X-API-SECRET.
- All calls are POST with JSON body {"query": "...", "variables": {...}}.
- `items(type: ItemType!, first, after)` -> ItemConnection; `type` is REQUIRED.
  `glossary(first, after)` is a typeless shortcut.
- `item(ref: ItemReference!)` fetches a single item by its UUID (or key).
  Unknown/deleted refs return a GraphQL error `code: ITEM_NOT_FOUND`. The server
  returns `data: null` for the WHOLE response if any aliased item errors (no
  partial data), so items are fetched one request each (bounded concurrency),
  tolerating not-found per item.
- Pagination (connections): Relay cursor via `pageInfo.endCursor`.

Item enrichment field mapping (Item -> items), confirmed:
    id                          -> id              (upsert key)
    key                         -> key
    name                        -> name
    type                        -> item_type
    descriptionV2.summary (fallback content.content) -> description
    descriptionV2.content.contentType                -> description_type (RAW/HTML)
    connection(ref:"curators")[0].node.id   -> owner_id
    connection(ref:"curators")[0].node.name -> owner_name
    connection(ref:"curators")[0].node.key  -> owner_email (curator is a contact;
                                               its key is the email)
    lastCatalogMetadataUpdate   -> last_updated_at
    completion, lifecycleStage, shared, catalogCode, orphan,
        descriptionV2.lifecycle -> attributes
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.api import ApiError

logger = logging.getLogger(__name__)

CATALOG_PATH = "/api/catalog/graphql"
ITEM_NOT_FOUND = "ITEM_NOT_FOUND"

# Item interface fields for connection listing.
_ITEM_FIELDS = """
    id
    type
    key
    name
    completion
    descriptionV2 { content { content contentType } summary lifecycle }
    lastCatalogMetadataUpdate
    catalogCode
    lifecycleStage
    shared
    orphan
"""

# Detail fields for a single item, including the curators (owner) connection.
_ITEM_DETAIL_FIELDS = """
    id
    key
    name
    type
    completion
    lastCatalogMetadataUpdate
    catalogCode
    lifecycleStage
    shared
    orphan
    descriptionV2 { content { content contentType } summary lifecycle }
    curators: connection(ref: "curators") { edges { node { id name key type } } }
"""

_ITEMS_QUERY = f"""
query Items($type: ItemType!, $first: Int!, $after: String) {{
  items(type: $type, first: $first, after: $after) {{
    totalCount
    pageInfo {{ hasNextPage endCursor }}
    edges {{ node {{ {_ITEM_FIELDS} }} }}
  }}
}}
"""

_GLOSSARY_QUERY = f"""
query Glossary($first: Int!, $after: String) {{
  glossary(first: $first, after: $after) {{
    totalCount
    pageInfo {{ hasNextPage endCursor }}
    edges {{ node {{ {_ITEM_FIELDS} }} }}
  }}
}}
"""

_SINGLE_ITEM_QUERY = f"""
query FetchItem($r: ItemReference!) {{
  item(ref: $r) {{ {_ITEM_DETAIL_FIELDS} }}
}}
"""

# Items are fetched one per request: the API returns `data: null` for an entire
# batch if any aliased item is ITEM_NOT_FOUND (no partial data), so per-item
# requests isolate deleted items. Bounded concurrency keeps throughput up.
_FETCH_CONCURRENCY = 8


class CatalogClient:
    """Client for the GraphQL Catalog API. Handles Relay cursor pagination."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0) -> None:
        """Store the instance base URL and API key for X-API-SECRET auth."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        """Auth + content headers for every GraphQL request."""
        return {
            "X-API-SECRET": self._api_key,
            "Content-Type": "application/json",
        }

    async def iter_items(
        self, item_type: str, *, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield catalog item nodes of a given `item_type`, paging via endCursor.

        Raises:
            ApiError: on HTTP, transport, or GraphQL errors (already logged).
        """
        async for node in self._iter_connection(
            _ITEMS_QUERY, "items", {"type": item_type, "first": page_size}
        ):
            yield node

    async def iter_glossary(
        self, *, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield glossary item nodes, paging via endCursor.

        Raises:
            ApiError: on HTTP, transport, or GraphQL errors (already logged).
        """
        async for node in self._iter_connection(
            _GLOSSARY_QUERY, "glossary", {"first": page_size}
        ):
            yield node

    async def fetch_items(
        self, refs: Sequence[str], *, concurrency: int = _FETCH_CONCURRENCY
    ) -> dict[str, dict[str, Any]]:
        """Fetch items by reference (UUID or key), one request per item.

        Deleted/unknown refs are skipped (ITEM_NOT_FOUND tolerated). Requests run
        with bounded concurrency. Returns a mapping of ref -> item node for those
        that exist.

        Raises:
            ApiError: on HTTP/transport errors or non-not-found GraphQL errors.
        """
        results: dict[str, dict[str, Any]] = {}
        semaphore = asyncio.Semaphore(concurrency)
        async with httpx.AsyncClient(
            base_url=self._base_url, headers=self._headers(), timeout=self._timeout
        ) as client:

            async def fetch_one(ref: str) -> None:
                async with semaphore:
                    data = await self._graphql(
                        client,
                        _SINGLE_ITEM_QUERY,
                        {"r": ref},
                        tolerate_not_found=True,
                    )
                node = data.get("item")
                if node:
                    results[ref] = node

            await asyncio.gather(*(fetch_one(ref) for ref in refs))
        return results

    async def _iter_connection(
        self, query: str, field: str, variables: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Drive Relay pagination over a connection field, yielding edge nodes."""
        after: str | None = None
        async with httpx.AsyncClient(
            base_url=self._base_url, headers=self._headers(), timeout=self._timeout
        ) as client:
            while True:
                data = await self._graphql(client, query, {**variables, "after": after})
                connection = data.get(field) or {}
                for edge in connection.get("edges") or []:
                    node = edge.get("node")
                    if node is not None:
                        yield node

                page_info = connection.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    break
                after = page_info.get("endCursor")
                if not after:
                    break

    async def _graphql(
        self,
        client: httpx.AsyncClient,
        query: str,
        variables: dict[str, Any],
        *,
        tolerate_not_found: bool = False,
    ) -> dict[str, Any]:
        """POST a GraphQL query; raise ApiError (after logging) on any failure.

        When ``tolerate_not_found`` is True, GraphQL errors that are all
        ITEM_NOT_FOUND are logged and ignored (partial data is returned).
        """
        try:
            response = await client.post(
                CATALOG_PATH, json={"query": query, "variables": variables}
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Catalog API returned HTTP %s: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise ApiError("Catalog API request failed") from exc
        except httpx.HTTPError as exc:
            logger.error("Catalog API request error: %s", exc)
            raise ApiError("Catalog API request failed") from exc

        body = response.json()
        errors = body.get("errors")
        if errors:
            only_not_found = all(
                (e.get("extensions") or {}).get("code") == ITEM_NOT_FOUND
                for e in errors
            )
            if tolerate_not_found and only_not_found:
                logger.info("Catalog: %s item(s) not found (skipped)", len(errors))
            else:
                logger.error("Catalog API GraphQL errors: %s", errors)
                raise ApiError("Catalog API returned GraphQL errors")
        return body.get("data") or {}
