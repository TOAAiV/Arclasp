"""
Safety-gating logic for mutating end-to-end tests against a real backend
database. Pure functions with no I/O or private-backend dependency — kept
separate so `test_e2e_database_safety.py` can exercise them without a live
backend checked out.

Mutating E2E tests must never run against a database that isn't explicitly
opted into via ARCLASP_E2E_DATABASE_URL + ARCLASP_E2E_ALLOW_MUTATION=1, and
must refuse to run if that URL coincides with a general DATABASE_URL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import unquote, urlsplit

E2E_DATABASE_URL_ENV = "ARCLASP_E2E_DATABASE_URL"
E2E_ALLOW_MUTATION_ENV = "ARCLASP_E2E_ALLOW_MUTATION"


class E2EDatabaseConfigError(RuntimeError):
    """Raised when the mutating E2E database target is unsafe."""

    def __init__(self, message: str, *, skip: bool = False) -> None:
        super().__init__(message)
        self.skip = skip


@dataclass(frozen=True)
class _DatabaseIdentity:
    scheme: str
    hostname: str
    port: int | None
    database: str
    username: str

    def describe(self) -> str:
        port = "" if self.port is None else f":{self.port}"
        user = "" if not self.username else f"{self.username}@"
        db = self.database or "/"
        return f"{self.scheme}://{user}{self.hostname}{port}/{db}"


def _database_identity(db_url: str, *, label: str) -> _DatabaseIdentity:
    parsed = urlsplit(db_url)
    if not parsed.scheme or not parsed.hostname:
        raise E2EDatabaseConfigError(
            f"{label} is malformed or missing a host"
        )
    database = parsed.path.lstrip("/")
    if not database:
        raise E2EDatabaseConfigError(
            f"{label} is malformed or missing a database name"
        )
    try:
        port = parsed.port
    except ValueError:
        raise E2EDatabaseConfigError(
            f"{label} is malformed or has an invalid port"
        ) from None
    return _DatabaseIdentity(
        scheme=parsed.scheme.lower(),
        hostname=parsed.hostname.lower(),
        port=port,
        database=unquote(database),
        username=unquote(parsed.username or ""),
    )


def _resolve_e2e_database_url(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    e2e_url = (env.get(E2E_DATABASE_URL_ENV) or "").strip()
    if not e2e_url:
        raise E2EDatabaseConfigError(
            f"{E2E_DATABASE_URL_ENV} is required for mutating SDK E2E tests",
            skip=True,
        )
    if env.get(E2E_ALLOW_MUTATION_ENV) != "1":
        raise E2EDatabaseConfigError(
            f"{E2E_ALLOW_MUTATION_ENV}=1 is required for mutating SDK E2E tests",
            skip=True,
        )

    e2e_identity = _database_identity(e2e_url, label=E2E_DATABASE_URL_ENV)
    general_url = (env.get("DATABASE_URL") or "").strip()
    if general_url:
        general_identity = _database_identity(general_url, label="DATABASE_URL")
        if e2e_identity == general_identity:
            raise E2EDatabaseConfigError(
                "Refusing mutating SDK E2E because the explicit E2E database "
                f"matches DATABASE_URL ({e2e_identity.describe()})"
            )
    return e2e_url
