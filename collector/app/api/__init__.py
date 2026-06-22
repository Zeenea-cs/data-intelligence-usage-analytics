"""API client package.

Clients are implemented only after the API discovery protocol (CLAUDE.md) has
been completed for each API; the confirmed contracts live in `DISCOVERY.md`.
REST clients (audit, scim) and GraphQL clients (catalog, user management) must
not share request patterns.
"""

from __future__ import annotations


class ApiError(RuntimeError):
    """Raised when an upstream API call fails after being logged.

    Clients log the underlying HTTP/transport/GraphQL error and raise this so a
    caller (e.g. a collector) can mark a run partial without the original
    exception type leaking through the abstraction.
    """
