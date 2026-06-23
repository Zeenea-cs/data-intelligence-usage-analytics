"""User Management (GraphQL) and SCIM (REST) API client.

Feature: enumerate users and resolve their permission sets and SCIM groups.
Confirmed contract (DISCOVERY.md §2, §3):

User Management -- `POST /public-api/catalog/graphql`, header X-API-SECRET.
Only three operations are used:
  1. mutation `createAllUsersExport { exportId }`;
  2. query `loadUsersExportStatus(input:{exportId}) { exportId status url }` --
     poll until status == DONE; `url` is a presigned S3 link returning CSV;
  3. query `listPermissionSets { ... }` -- resolves the export's permission set.
There is no list-users query, so enumeration is the CSV export.

SCIM -- `GET /api/scim/v2/Users`, **Bearer** auth (not X-API-SECRET).
Pagination is `startIndex` (1-based) + `count`.

Field mapping (-> users) is applied in the collector; see collectors/users.py.
"""

from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.api import ApiError
from app.api._http import (
    bearer_headers,
    graphql,
    make_client,
    secret_client,
    send_json,
    send_text,
)

USERS_GRAPHQL_PATH = "/public-api/catalog/graphql"
SCIM_USERS_PATH = "/api/scim/v2/Users"

_CREATE_EXPORT_MUTATION = "mutation { createAllUsersExport { exportId } }"
_EXPORT_STATUS_QUERY = (
    "query ExportStatus($input: LoadUsersExportStatusInput!) {"
    " loadUsersExportStatus(input: $input) { exportId status url } }"
)
_PERMISSION_SETS_QUERY = (
    "{ listPermissionSets { id name description builtIn licenseType"
    " permissions { permission } } }"
)

_STATUS_DONE = "DONE"
_STATUS_ERROR = "ERROR"


class UsersClient:
    """User Management bulk export + permission sets, plus SCIM user listing."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
        max_polls: int = 60,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._poll_interval = poll_interval  # seconds between export-status polls
        self._max_polls = max_polls  # give up after this many polls

    def _graphql_client(self) -> httpx.AsyncClient:
        return secret_client(self._base_url, self._api_key, self._timeout)

    # -- User Management: bulk export -----------------------------------

    async def export_all_users(self) -> list[dict[str, str]]:
        """Run the async export and return the parsed CSV rows.

        Flow: createAllUsersExport -> poll loadUsersExportStatus until DONE ->
        download the presigned CSV -> parse into dict rows keyed by CSV header.

        Raises:
            ApiError: on upstream failure or an ERROR / timed-out export.
        """
        async with self._graphql_client() as client:
            data = await graphql(
                client, USERS_GRAPHQL_PATH, _CREATE_EXPORT_MUTATION, label="User Management API"
            )
            export_id = (data.get("createAllUsersExport") or {}).get("exportId")
            if not export_id:
                raise ApiError("User export did not return an exportId")
            url = await self._poll_export(client, export_id)

        return await self._download_csv(url)

    async def _poll_export(self, client: Any, export_id: str) -> str:
        """Poll the export status until DONE; return the download URL."""
        variables = {"input": {"exportId": export_id}}
        for _ in range(self._max_polls):
            data = await graphql(
                client,
                USERS_GRAPHQL_PATH,
                _EXPORT_STATUS_QUERY,
                label="User Management API",
                variables=variables,
            )
            status_obj = data.get("loadUsersExportStatus") or {}
            status = status_obj.get("status")
            if status == _STATUS_DONE:
                if not status_obj.get("url"):
                    raise ApiError("User export DONE but no url provided")
                return status_obj["url"]
            if status == _STATUS_ERROR:
                raise ApiError("User export failed (status ERROR)")
            await asyncio.sleep(self._poll_interval)
        raise ApiError("User export did not complete within the poll budget")

    async def _download_csv(self, url: str) -> list[dict[str, str]]:
        """GET the presigned export URL (absolute, unauthenticated) and parse CSV."""
        text = await send_text(url, label="User export download", timeout=self._timeout)
        reader = csv.DictReader(io.StringIO(text))
        return list(reader)

    # -- User Management: permission sets -------------------------------

    async def list_permission_sets(self) -> list[dict[str, Any]]:
        """Return the permission-set catalogue (to resolve the export's set id).

        Raises:
            ApiError: on upstream failure (already logged).
        """
        async with self._graphql_client() as client:
            data = await graphql(
                client, USERS_GRAPHQL_PATH, _PERMISSION_SETS_QUERY, label="User Management API"
            )
        return data.get("listPermissionSets") or []

    # -- SCIM ------------------------------------------------------------

    async def iter_scim_users(
        self, *, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield SCIM user resources, paging with startIndex/count (Bearer auth).

        Raises:
            ApiError: on upstream failure (already logged).
        """
        start_index = 1
        headers = bearer_headers(self._api_key)
        async with make_client(self._base_url, headers, self._timeout) as client:
            while True:
                body = await send_json(
                    client,
                    "GET",
                    SCIM_USERS_PATH,
                    label="SCIM API",
                    params={"startIndex": start_index, "count": page_size},
                )
                resources = body.get("Resources") or []
                if not resources:
                    break
                for resource in resources:
                    yield resource

                # A short page means the last page -- the reliable stop signal.
                # Otherwise advance by the page actually returned, and also stop
                # once we've walked past `totalResults` when the server reports
                # it (a server omitting it is handled by the short-page check).
                if len(resources) < page_size:
                    break
                next_index = body.get("startIndex", start_index) + len(resources)
                total = body.get("totalResults")
                if total is not None and next_index > total:
                    break
                start_index = next_index
