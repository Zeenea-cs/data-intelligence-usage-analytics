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
DEFAULT_POSTGRES_HOST = "db"
DEFAULT_POSTGRES_PORT = 5432
DEFAULT_POSTGRES_DB = "actian_companion"
DEFAULT_POSTGRES_USER = "actian"
DEFAULT_LOG_LEVEL = "INFO"


@dataclass(frozen=True)
class Settings:
    """Resolved, validated application settings.

    Attributes mirror the configuration parameters documented in CLAUDE.md.
    """

    actian_instance_url: str
    actian_api_key: str
    collect_cron: str
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str
    metabase_db_password: str
    log_level: str

    @property
    def database_url(self) -> str:
        """Return the SQLAlchemy connection URL for the companion database.

        Uses the ``postgresql+psycopg`` dialect, which psycopg 3 serves for both
        synchronous (Alembic) and asynchronous (collectors) engines.
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

    port_raw = os.environ.get("POSTGRES_PORT", str(DEFAULT_POSTGRES_PORT))
    try:
        postgres_port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(
            f"POSTGRES_PORT must be an integer, got {port_raw!r}."
        ) from exc

    return Settings(
        actian_instance_url=os.environ["ACTIAN_INSTANCE_URL"].rstrip("/"),
        actian_api_key=os.environ["ACTIAN_API_KEY"],
        collect_cron=os.environ.get("COLLECT_CRON", DEFAULT_COLLECT_CRON),
        postgres_host=os.environ.get("POSTGRES_HOST", DEFAULT_POSTGRES_HOST),
        postgres_port=postgres_port,
        postgres_db=os.environ.get("POSTGRES_DB", DEFAULT_POSTGRES_DB),
        postgres_user=os.environ.get("POSTGRES_USER", DEFAULT_POSTGRES_USER),
        postgres_password=os.environ["POSTGRES_PASSWORD"],
        metabase_db_password=os.environ["METABASE_DB_PASSWORD"],
        log_level=os.environ.get("LOG_LEVEL", DEFAULT_LOG_LEVEL),
    )
