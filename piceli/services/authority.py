"""Request identity and revocable application grants for a future cluster service.

The local service has its separate single-user boundary. Cluster callers must
set a verified principal for each request; absence of that principal fails
closed. Grants are installation configuration, never repository source data.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from piceli.services.contracts import Principal

_PRINCIPAL: ContextVar[Principal | None] = ContextVar(
    "piceli_ui_principal", default=None
)


@contextmanager
def request_principal(principal: Principal) -> Iterator[None]:
    token = _PRINCIPAL.set(principal)
    try:
        yield
    finally:
        _PRINCIPAL.reset(token)


def active_principal() -> Principal | None:
    return _PRINCIPAL.get()


class ScopePolicy:
    """Exact per-principal application/action grants with revocation epochs."""

    def __init__(
        self, grants: Mapping[str, Mapping[str, frozenset[str]]] | None = None
    ) -> None:
        self._lock = threading.RLock()
        self._grants = self._copy(grants or {})
        self._revision = 0

    @staticmethod
    def _copy(
        grants: Mapping[str, Mapping[str, frozenset[str]]],
    ) -> dict[str, dict[str, frozenset[str]]]:
        return {
            principal: {
                application: frozenset(actions)
                for application, actions in scopes.items()
            }
            for principal, scopes in grants.items()
        }

    def replace(self, grants: Mapping[str, Mapping[str, frozenset[str]]]) -> None:
        with self._lock:
            self._grants = self._copy(grants)
            self._revision += 1

    def allows(self, principal_id: str, application_id: str, action: str) -> bool:
        with self._lock:
            return action in self._grants.get(principal_id, {}).get(
                application_id, frozenset()
            )

    def has_grant(self, principal_id: str, applications: Collection[str]) -> bool:
        """Only configured applications with at least one action admit a login."""
        with self._lock:
            scopes = self._grants.get(principal_id, {})
            return any(bool(scopes.get(application)) for application in applications)

    def revision(self) -> int:
        with self._lock:
            return self._revision
