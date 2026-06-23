"""Tests for app.config: env loading and required-variable validation."""

from __future__ import annotations

import pytest

from app.config import Settings, load_settings

_REQUIRED = {
    "ACTIAN_INSTANCE_URL": "https://myorg.actian.com/",
    "ACTIAN_API_KEY": "secret-key",
    "POSTGRES_PASSWORD": "pw",
    "METABASE_DB_PASSWORD": "mb-pw",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Clear all known config vars, then set required ones plus overrides."""
    # Keep tests hermetic: do not let a real .env file repopulate the environment.
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: False)
    for name in (
        "ACTIAN_INSTANCE_URL",
        "ACTIAN_API_KEY",
        "COLLECT_CRON",
        "AUDIT_INITIAL_DAYS",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "METABASE_DB_PASSWORD",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    for key, value in {**_REQUIRED, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_load_settings_reads_required_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_settings returns populated Settings when all required vars are set."""
    _set_env(monkeypatch)
    settings = load_settings()
    assert isinstance(settings, Settings)
    assert settings.actian_api_key == "secret-key"
    # Trailing slash stripped from the instance URL.
    assert settings.actian_instance_url == "https://myorg.actian.com"
    # Optional vars fall back to documented defaults.
    assert settings.collect_cron == "0 0 * * *"
    assert settings.audit_initial_days == 365  # default look-back window
    assert settings.postgres_host == "db"
    assert settings.postgres_port == 5432
    assert settings.postgres_db == "actian_companion"
    assert settings.postgres_user == "actian"
    assert settings.log_level == "INFO"


def test_audit_initial_days_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUDIT_INITIAL_DAYS overrides the default look-back window."""
    _set_env(monkeypatch, AUDIT_INITIAL_DAYS="30")
    assert load_settings().audit_initial_days == 30


def test_database_url_uses_psycopg_dialect(monkeypatch: pytest.MonkeyPatch) -> None:
    """database_url is built from the postgres_* parts with the psycopg dialect."""
    _set_env(monkeypatch, POSTGRES_HOST="dbhost", POSTGRES_PORT="6543")
    settings = load_settings()
    assert settings.database_url == (
        "postgresql+psycopg://actian:pw@dbhost:6543/actian_companion"
    )


def test_load_settings_raises_on_missing_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_settings raises when a required variable is missing."""
    _set_env(monkeypatch)
    monkeypatch.delenv("ACTIAN_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ACTIAN_API_KEY"):
        load_settings()


def test_load_settings_raises_on_non_integer_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_settings raises a clear error when POSTGRES_PORT is not an integer."""
    _set_env(monkeypatch, POSTGRES_PORT="not-a-number")
    with pytest.raises(RuntimeError, match="POSTGRES_PORT"):
        load_settings()
