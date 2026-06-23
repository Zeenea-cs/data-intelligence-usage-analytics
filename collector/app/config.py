"""Application configuration.

Reads settings from environment variables (loadable from a `.env` file via
python-dotenv) and validates that required values are present. All secrets and
URLs must be sourced here -- no hardcoded credentials elsewhere in the codebase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Variables that must be set; load_settings raises if any are missing/empty.
REQUIRED_VARS: tuple[str, ...] = (
    "ACTIAN_INSTANCE_URL",
    "ACTIAN_API_KEY",
    "POSTGRES_PASSWORD",
    "METABASE_DB_PASSWORD",
)

# Defaults for optional variables.
DEFAULT_COLLECT_CRON = "0 0 * * *"
# Number of days of history to request when retrieving audit events (the `from`
# bound of the Audit API window).
DEFAULT_AUDIT_INITIAL_DAYS = 365
DEFAULT_POSTGRES_HOST = "db"
DEFAULT_POSTGRES_PORT = 5432
DEFAULT_POSTGRES_DB = "actian_companion"
DEFAULT_POSTGRES_USER = "actian"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_WEBUI_HOST = "0.0.0.0"
DEFAULT_WEBUI_PORT = 8000
# Shared directory (bind-mounted into every service) where each service writes
# its log file; the web UI tails these files.
DEFAULT_LOG_DIR = "/var/log/actian"
DEFAULT_LOG_MAX_BYTES = 5_000_000  # rotate the collector log past ~5 MB
DEFAULT_LOG_BACKUP_COUNT = 5  # keep this many rotated collector logs


@dataclass(frozen=True)
class Settings:
    """Resolved, validated application settings.

    Attributes mirror the configuration parameters documented in CLAUDE.md.
    """

    actian_instance_url: str
    actian_api_key: str
    collect_cron: str
    audit_initial_days: int
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str
    metabase_db_password: str
    log_level: str
    # Web UI bind address/port (fields with defaults come last).
    webui_host: str = DEFAULT_WEBUI_HOST
    webui_port: int = DEFAULT_WEBUI_PORT
    # Logging: shared log directory + collector-log rotation settings.
    log_dir: str = DEFAULT_LOG_DIR
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT

    @property
    def database_url(self) -> str:
        """Return the SQLAlchemy connection URL for the companion database.

        Uses the ``postgresql+psycopg`` dialect (psycopg 3), driving the async
        engine used by the collectors.
        """
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


def load_settings() -> Settings:
    """Load and validate settings from the environment.

    Loads a ``.env`` file if present, then reads every variable documented in
    CLAUDE.md. Optional variables fall back to their documented defaults.

    Raises:
        RuntimeError: if one or more required variables are missing or empty.
    """
    load_dotenv()

    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them in the environment or in a .env file "
            "(see .env.example)."
        )

    postgres_port = _int_env("POSTGRES_PORT", DEFAULT_POSTGRES_PORT)
    webui_port = _int_env("WEBUI_PORT", DEFAULT_WEBUI_PORT)

    return Settings(
        actian_instance_url=os.environ["ACTIAN_INSTANCE_URL"].rstrip("/"),
        actian_api_key=os.environ["ACTIAN_API_KEY"],
        collect_cron=os.environ.get("COLLECT_CRON", DEFAULT_COLLECT_CRON),
        audit_initial_days=_int_env("AUDIT_INITIAL_DAYS", DEFAULT_AUDIT_INITIAL_DAYS),
        postgres_host=os.environ.get("POSTGRES_HOST", DEFAULT_POSTGRES_HOST),
        postgres_port=postgres_port,
        postgres_db=os.environ.get("POSTGRES_DB", DEFAULT_POSTGRES_DB),
        postgres_user=os.environ.get("POSTGRES_USER", DEFAULT_POSTGRES_USER),
        postgres_password=os.environ["POSTGRES_PASSWORD"],
        metabase_db_password=os.environ["METABASE_DB_PASSWORD"],
        log_level=os.environ.get("LOG_LEVEL", DEFAULT_LOG_LEVEL),
        webui_host=os.environ.get("WEBUI_HOST", DEFAULT_WEBUI_HOST),
        webui_port=webui_port,
        log_dir=os.environ.get("LOG_DIR", DEFAULT_LOG_DIR),
        log_max_bytes=_int_env("LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES),
        log_backup_count=_int_env("LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT),
    )


def _int_env(name: str, default: int) -> int:
    """Read an integer env var, raising a clear error on a non-integer value."""
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}.") from exc
