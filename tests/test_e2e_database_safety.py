import pytest

from tests._e2e_database_safety import (
    E2EDatabaseConfigError,
    E2E_ALLOW_MUTATION_ENV,
    E2E_DATABASE_URL_ENV,
    _resolve_e2e_database_url,
)


def _pg_url(
    *,
    user: str = "postgres.test_project",
    password: str = "secret-password",
    host: str = "db.example.test",
    port: int = 5432,
    database: str = "postgres",
) -> str:
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"


def test_e2e_database_url_is_required() -> None:
    with pytest.raises(E2EDatabaseConfigError) as exc_info:
        _resolve_e2e_database_url({})

    assert exc_info.value.skip is True
    assert "DATABASE_URL" in str(exc_info.value)


def test_e2e_mutation_opt_in_is_required() -> None:
    with pytest.raises(E2EDatabaseConfigError) as exc_info:
        _resolve_e2e_database_url({E2E_DATABASE_URL_ENV: _pg_url()})

    assert exc_info.value.skip is True
    assert E2E_ALLOW_MUTATION_ENV in str(exc_info.value)


def test_e2e_database_must_not_equal_general_database_url() -> None:
    url = _pg_url(password="e2e-secret")

    with pytest.raises(E2EDatabaseConfigError) as exc_info:
        _resolve_e2e_database_url(
            {
                E2E_DATABASE_URL_ENV: url,
                E2E_ALLOW_MUTATION_ENV: "1",
                "DATABASE_URL": _pg_url(password="prod-secret"),
            }
        )

    message = str(exc_info.value)
    assert exc_info.value.skip is False
    assert "matches DATABASE_URL" in message
    assert "e2e-secret" not in message
    assert "prod-secret" not in message


def test_explicit_distinct_e2e_database_with_opt_in_is_accepted() -> None:
    url = _pg_url(host="db.e2e.example.test")

    resolved = _resolve_e2e_database_url(
        {
            E2E_DATABASE_URL_ENV: url,
            E2E_ALLOW_MUTATION_ENV: "1",
            "DATABASE_URL": _pg_url(host="db.production.example.test"),
        }
    )

    assert resolved == url


def test_malformed_e2e_database_url_refuses_safely() -> None:
    with pytest.raises(E2EDatabaseConfigError) as exc_info:
        _resolve_e2e_database_url(
            {
                E2E_DATABASE_URL_ENV: "not-a-database-url",
                E2E_ALLOW_MUTATION_ENV: "1",
            }
        )

    assert exc_info.value.skip is False
    assert E2E_DATABASE_URL_ENV in str(exc_info.value)


def test_invalid_general_database_url_refuses_safely() -> None:
    with pytest.raises(E2EDatabaseConfigError) as exc_info:
        _resolve_e2e_database_url(
            {
                E2E_DATABASE_URL_ENV: _pg_url(),
                E2E_ALLOW_MUTATION_ENV: "1",
                "DATABASE_URL": "postgresql://user:pw@db.example.test:bad/postgres",
            }
        )

    assert exc_info.value.skip is False
    assert "DATABASE_URL" in str(exc_info.value)
    assert "pw" not in str(exc_info.value)
