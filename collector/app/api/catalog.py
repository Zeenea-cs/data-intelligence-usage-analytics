"""Catalog API client (GraphQL).

Feature: read catalog items. Confirmed contract (DISCOVERY.md §4):

* `POST /api/catalog/graphql`; header X-API-SECRET; body `{query, variables}`.
* `item(ref: ItemReference!)` fetches one item by UUID or key; unknown/deleted
  refs return GraphQL error `code: ITEM_NOT_FOUND`.
* No partial data: any not-found in a multi-item query nulls the WHOLE response,
  so items are fetched one request each (bounded concurrency), tolerating
  not-found per item.
* Owner = first node of `connection(ref: "curators")` (a contact whose `key` is
  the email). Description type = `descriptionV2.content.contentType` (RAW/HTML).
* `items(type:)` / `glossary(...)` are Relay connections (cursor pagination via
  `pageInfo.endCursor`); kept available though enrichment uses single fetch.

Field mapping (Item -> items) is applied in the collector; see collectors/items.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.api._http import graphql, make_client, secret_headers

CATALOG_PATH = "/api/catalog/graphql"
ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
# Default number of concurrent single-item fetches (see fetch_items).
FETCH_CONCURRENCY = 8

# Item interface fields used for connection listing.
_ITEM_FIELDS = """
    id type key name completion
    descriptionV2 { content { content contentType } summary lifecycle }
    lastCatalogMetadataUpdate catalogCode lifecycleStage shared orphan
"""

# Detail fields for a single item, including the curators (owner) connection.
_ITEM_DETAIL_FIELDS = """
    id key name type completion
    lastCatalogMetadataUpdate catalogCode lifecycleStage shared orphan
    descriptionV2 { content { content contentType } summary lifecycle }
    curators: connection(ref: "curators") { edges { node { id name key type } } }
"""

_ITEMS_QUERY = f"""
query Items($type: ItemType!, $first: Int!, $after: String) {{
  items(type: $type, first: $first, after: $after) {{
    pageInfo {{ hasNextPage endCursor }}
    edges {{ node {{ {_ITEM_FIELDS} }} }}
  }}
}}
"""

_GLOSSARY_QUERY = f"""
query Glossary($first: Int!, $after: String) {{
  glossary(first: $first, after: $after) {{
    pageInfo {{ hasNextPage endCursor }}
    edges {{ node {{ {_ITEM_FIELDS} }} }}
  }}
}}
"""

_SINGLE_ITEM_QUERY = f"query FetchItem($r: ItemReference!) {{ item(ref: $r) {{ {_ITEM_DETAIL_FIELDS} }} }}"


class CatalogClient:
    """Reads catalog items: single-item fetch (enrichment) + connection listing."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _client(self) -> Any:
        return make_client(
            self._base_url, secret_headers(self._api_key, json_body=True), self._timeout
        )

    async def fetch_items(
        self, refs: Sequence[str], *, concurrency: int = FETCH_CONCURRENCY
    ) -> dict[str, dict[str, Any]]:
        """Fetch items by ref (UUID/key), one request each, skipping not-found.

        Returns a mapping of ref -> item node for those that exist. Requests run
        with bounded concurrency.

        Raises:
            ApiError: on transport/HTTP errors or non-not-found GraphQL errors.
        """
        results: dict[str, dict[str, Any]] = {}
        semaphore = asyncio.Semaphore(concurrency)
        async with self._client() as client:

            async def fetch_one(ref: str) -> None:
                async with semaphore:
                    data = await graphql(
                        client,
                        CATALOG_PATH,
                        _SINGLE_ITEM_QUERY,
                        label="Catalog API",
                        variables={"r": ref},
                        tolerated_error_code=ITEM_NOT_FOUND,
                    )
                node = data.get("item")
                if node:
                    results[ref] = node

            await asyncio.gather(*(fetch_one(ref) for ref in refs))
        return results

    async def iter_items(
        self, item_type: str, *, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield item nodes of one `item_type`, paging via `endCursor`."""
        async for node in self._iter_connection(
            _ITEMS_QUERY, "items", {"type": item_type, "first": page_size}
        ):
            yield node

    async def iter_glossary(
        self, *, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield glossary item nodes, paging via `endCursor`."""
        async for node in self._iter_connection(
            _GLOSSARY_QUERY, "glossary", {"first": page_size}
        ):
            yield node

    async def _iter_connection(
        self, query: str, field: str, variables: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Drive Relay cursor pagination over a connection field, yielding nodes."""
        after: str | None = None
        async with self._client() as client:
            while True:
                data = await graphql(
                    client,
                    CATALOG_PATH,
                    query,
                    label="Catalog API",
                    variables={**variables, "after": after},
                )
                connection = data.get(field) or {}
                for edge in connection.get("edges") or []:
                    if edge.get("node") is not None:
                        yield edge["node"]

                page_info = connection.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    break
                after = page_info.get("endCursor")
                if not after:
                    break
