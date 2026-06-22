"""User Management (GraphQL) and SCIM (REST) API client.

Confirmed contract (see DISCOVERY.md §3, §2 and the consolidated plan):

User Management API — POST /public-api/catalog/graphql, auth X-API-SECRET.
Only THREE operations are in scope:
  1. mutation `createAllUsersExport { exportId }`
  2. query `loadUsersExportStatus(input:{exportId}) { exportId status url }`
     -- poll until status == DONE (enum RUNNING | DONE | ERROR); `url` is a
     presigned S3 link returning CSV.
  3. query `listPermissionSets { id name description builtIn licenseType
     permissions { permission } }` -- resolves the export's `Permission set id`.

SCIM API — GET /api/scim/v2/Users. Auth is `Authorization: Bearer {key}`
(NOT X-API-SECRET). Pagination: `startIndex` (1-based) + `count`; response gives
`totalResults`, `itemsPerPage`, `startIndex`. Stop when no Resources remain.

Field mapping (-> users), confirmed:
    export Id                         -> id            (upsert key; == SCIM id)
    export Email                      -> username, email
    export First name + Last name     -> display_name
    export License type == "Steward"  -> is_steward    (applied in collector)
    export Permission set id -> listPermissionSets -> roles (resolved set, JSONB)
    export activity + SCIM groups/active -> attributes  (JSONB)
    (collector)                       -> first_seen_at / last_updated_at
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.api import ApiError

logger = logging.getLogger(__name__)

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
    """Client combining the GraphQL User Management API and the REST SCIM API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
        max_polls: int = 60,
    ) -> None:
        """Store the instance base URL, API key, and export-poll settings.

        Args:
            poll_interval: seconds between export-status polls.
            max_polls: maximum status polls before giving up.
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._max_polls = max_polls

    # -- header builders -------------------------------------------------

    def _graphql_headers(self) -> dict[str, str]:
        """X-API-SECRET auth for the User Management GraphQL API."""
        return {
            "X-API-SECRET": self._api_key,
            "Content-Type": "application/json",
        }

    def _scim_headers(self) -> dict[str, str]:
        """Bearer auth for the SCIM API (NOT X-API-SECRET)."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/scim+json",
        }

    # -- User Management: bulk export ------------------------------------

    async def export_all_users(self) -> list[dict[str, str]]:
        """Run the export flow and return the parsed CSV rows.

        Steps: createAllUsersExport -> poll loadUsersExportStatus until DONE ->
        download the presigned CSV and parse it into dict rows keyed by the
        exact CSV column headers.

        Raises:
            ApiError: on HTTP/transport/GraphQL failure or an ERROR/timed-out
                export (already logged).
        """
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._graphql_headers(),
            timeout=self._timeout,
        ) as client:
            data = await self._graphql(client, _CREATE_EXPORT_MUTATION)
            export_id = (data.get("createAllUsersExport") or {}).get("exportId")
            if not export_id:
                logger.error("createAllUsersExport returned no exportId: %s", data)
                raise ApiError("User export did not return an exportId")
            url = await self._poll_export(client, export_id)

        return await self._download_csv(url)

    async def _poll_export(self, client: httpx.AsyncClient, export_id: str) -> str:
        """Poll loadUsersExportStatus until DONE; return the download URL."""
        variables = {"input": {"exportId": export_id}}
        for _ in range(self._max_polls):
            data = await self._graphql(client, _EXPORT_STATUS_QUERY, variables)
            status_obj = data.get("loadUsersExportStatus") or {}
            status = status_obj.get("status")
            if status == _STATUS_DONE:
                url = status_obj.get("url")
                if not url:
                    raise ApiError("User export DONE but no url provided")
                return url
            if status == _STATUS_ERROR:
                logger.error("User export %s failed with status ERROR", export_id)
                raise ApiError("User export failed (status ERROR)")
            await asyncio.sleep(self._poll_interval)
        raise ApiError("User export did not complete within the poll budget")

    async def _download_csv(self, url: str) -> list[dict[str, str]]:
        """GET the presigned export URL (no auth) and parse the CSV."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "User export download HTTP %s: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise ApiError("User export download failed") from exc
        except httpx.HTTPError as exc:
            logger.error("User export download error: %s", exc)
            raise ApiError("User export download failed") from exc

        reader = csv.DictReader(io.StringIO(response.text))
        return list(reader)

    # -- User Management: permission sets --------------------------------

    async def list_permission_sets(self) -> list[dict[str, Any]]:
        """Return the permission-set catalog (to resolve export `Permission set id`).

        Raises:
            ApiError: on HTTP/transport/GraphQL failure (already logged).
        """
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._graphql_headers(),
            timeout=self._timeout,
        ) as client:
            data = await self._graphql(client, _PERMISSION_SETS_QUERY)
        return data.get("listPermissionSets") or []

    # -- SCIM ------------------------------------------------------------

    async def iter_scim_users(
        self, *, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield SCIM user resources, paging with startIndex/count.

        Raises:
            ApiError: on HTTP or transport failure (already logged).
        """
        start_index = 1
        async with httpx.AsyncClient(
            base_url=self._base_url, headers=self._scim_headers(), timeout=self._timeout
        ) as client:
            while True:
                body = await self._scim_get(
                    client, {"startIndex": start_index, "count": page_size}
                )
                resources = body.get("Resources") or []
                if not resources:
                    break
                for resource in resources:
                    yield resource

                total = body.get("totalResults", 0)
                next_index = body.get("startIndex", start_index) + len(resources)
                if next_index > total:
                    break
                start_index = next_index

    async def _scim_get(
        self, client: httpx.AsyncClient, params: dict[str, Any]
    ) -> dict[str, Any]:
        """GET one SCIM page; raise ApiError (after logging) on failure."""
        try:
            response = await client.get(SCIM_USERS_PATH, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "SCIM API returned HTTP %s: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise ApiError("SCIM API request failed") from exc
        except httpx.HTTPError as exc:
            logger.error("SCIM API request error: %s", exc)
            raise ApiError("SCIM API request failed") from exc
        return response.json()

    # -- shared GraphQL --------------------------------------------------

    async def _graphql(
        self,
        client: httpx.AsyncClient,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST a GraphQL op; raise ApiError (after logging) on any failure."""
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        try:
            response = await client.post(USERS_GRAPHQL_PATH, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "User Management API returned HTTP %s: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise ApiError("User Management API request failed") from exc
        except httpx.HTTPError as exc:
            logger.error("User Management API request error: %s", exc)
            raise ApiError("User Management API request failed") from exc

        body = response.json()
        if body.get("errors"):
            logger.error("User Management API GraphQL errors: %s", body["errors"])
            raise ApiError("User Management API returned GraphQL errors")
        return body.get("data") or {}
