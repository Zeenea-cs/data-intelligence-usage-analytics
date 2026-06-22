"""Factory bundling the three Actian API clients built from configuration.

Keeps client construction in one place so the scheduler does not repeat the
base-URL / API-key wiring.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.api.audit import AuditClient
from app.api.catalog import CatalogClient
from app.api.users import UsersClient
from app.config import Settings


@dataclass(frozen=True)
class ActianClients:
    """The three API clients used by the collectors."""

    users: UsersClient
    audit: AuditClient
    catalog: CatalogClient


def build_clients(settings: Settings) -> ActianClients:
    """Construct all API clients from the instance URL + API key in settings."""
    base, key = settings.actian_instance_url, settings.actian_api_key
    return ActianClients(
        users=UsersClient(base, key),
        audit=AuditClient(base, key),
        catalog=CatalogClient(base, key),
    )
