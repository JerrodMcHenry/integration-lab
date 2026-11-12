"""Environment-based configuration.

All settings come from environment variables (optionally loaded from a local
``.env`` file). Defaults are safe for local development only. In a production
environment, startup fails if any dev-default secret is still in use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from app.integrations.retry import MAX_RETRIES_CEILING

PRODUCTION_ENVS = frozenset({"production", "prod", "staging"})

# Dev-only defaults. They match the mock services and are rejected in production.
DEV_CRM_CLIENT_SECRET = "dev-only-secret"
DEV_INTERNAL_API_KEY = "dev-only-internal-key"


class ConfigurationError(RuntimeError):
    """Raised when the environment configuration is unsafe or invalid."""


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _validate_http(prefix: str, timeout: float, retries: int, base: float, cap: float) -> None:
    if timeout <= 0:
        raise ValueError(f"{prefix}_TIMEOUT_SECONDS must be positive")
    if not 0 <= retries <= MAX_RETRIES_CEILING:
        # Hard ceiling: retries inside clients must stay bounded.
        raise ValueError(f"{prefix}_MAX_RETRIES must be between 0 and {MAX_RETRIES_CEILING}")
    if base < 0 or cap < 0:
        raise ValueError(f"{prefix} backoff values must be non-negative")


@dataclass(frozen=True)
class CRMSettings:
    base_url: str
    token_url: str
    client_id: str
    client_secret: str
    timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float

    def __post_init__(self) -> None:
        _validate_http("CRM", self.timeout_seconds, self.max_retries,
                       self.backoff_base_seconds, self.backoff_max_seconds)


@dataclass(frozen=True)
class InternalSystemSettings:
    base_url: str
    api_key: str
    timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float

    def __post_init__(self) -> None:
        _validate_http("INTERNAL_API", self.timeout_seconds, self.max_retries,
                       self.backoff_base_seconds, self.backoff_max_seconds)


@dataclass(frozen=True)
class DatabaseSettings:
    #: SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@host:5432/db. ``None``
    #: selects in-memory storage (development/test only).
    url: str | None
    pool_size: int = 10
    max_overflow: int = 10
    pool_timeout_seconds: float = 5.0
    connect_timeout_seconds: int = 3
    #: Max time to wait for another worker's sync of the same account.
    lock_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.url is not None and not self.url.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        if self.pool_size < 1 or self.max_overflow < 0:
            raise ValueError("DB_POOL_SIZE must be >= 1 and DB_MAX_OVERFLOW >= 0")
        if not 0 < self.lock_timeout_seconds <= 300:
            raise ValueError("SYNC_LOCK_TIMEOUT_SECONDS must be in (0, 300]")
        if self.connect_timeout_seconds < 1:
            raise ValueError("DB_CONNECT_TIMEOUT_SECONDS must be >= 1")


@dataclass(frozen=True)
class Settings:
    environment: str
    crm: CRMSettings
    internal: InternalSystemSettings
    database: DatabaseSettings
    mapping_config_dir: Path
    #: Single-tenant placeholder until authentication lands in Milestone 5.
    default_tenant_id: str

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in PRODUCTION_ENVS

    def __post_init__(self) -> None:
        if self.is_production:
            problems = []
            if not self.database.url:
                problems.append("DATABASE_URL is required in production")
            if self.crm.client_secret == DEV_CRM_CLIENT_SECRET:
                problems.append("CRM_CLIENT_SECRET uses the development default")
            if self.internal.api_key == DEV_INTERNAL_API_KEY:
                problems.append("INTERNAL_API_KEY uses the development default")
            for name, url in (("CRM_BASE_URL", self.crm.base_url),
                              ("INTERNAL_API_BASE_URL", self.internal.base_url)):
                if not url.startswith("https://"):
                    problems.append(f"{name} must use https in production")
            if problems:
                raise ConfigurationError("Unsafe production configuration: " + "; ".join(problems))


def load_settings() -> Settings:
    """Build settings from the current environment (uncached)."""
    load_dotenv()
    crm_base = os.getenv("CRM_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    return Settings(
        environment=os.getenv("APP_ENV", "development"),
        crm=CRMSettings(
            base_url=crm_base,
            token_url=os.getenv("CRM_TOKEN_URL", f"{crm_base}/oauth/token"),
            client_id=os.getenv("CRM_CLIENT_ID", "integrationlab-dev"),
            client_secret=os.getenv("CRM_CLIENT_SECRET", DEV_CRM_CLIENT_SECRET),
            timeout_seconds=_env_float("CRM_TIMEOUT_SECONDS", 5.0),
            max_retries=_env_int("CRM_MAX_RETRIES", 2),
            backoff_base_seconds=_env_float("CRM_BACKOFF_BASE_SECONDS", 0.5),
            backoff_max_seconds=_env_float("CRM_BACKOFF_MAX_SECONDS", 4.0),
        ),
        internal=InternalSystemSettings(
            base_url=os.getenv("INTERNAL_API_BASE_URL", "http://127.0.0.1:8002").rstrip("/"),
            api_key=os.getenv("INTERNAL_API_KEY", DEV_INTERNAL_API_KEY),
            timeout_seconds=_env_float("INTERNAL_API_TIMEOUT_SECONDS", 5.0),
            max_retries=_env_int("INTERNAL_API_MAX_RETRIES", 2),
            backoff_base_seconds=_env_float("INTERNAL_API_BACKOFF_BASE_SECONDS", 0.5),
            backoff_max_seconds=_env_float("INTERNAL_API_BACKOFF_MAX_SECONDS", 4.0),
        ),
        database=DatabaseSettings(
            url=os.getenv("DATABASE_URL") or None,
            pool_size=_env_int("DB_POOL_SIZE", 10),
            max_overflow=_env_int("DB_MAX_OVERFLOW", 10),
            pool_timeout_seconds=_env_float("DB_POOL_TIMEOUT_SECONDS", 5.0),
            connect_timeout_seconds=_env_int("DB_CONNECT_TIMEOUT_SECONDS", 3),
            lock_timeout_seconds=_env_float("SYNC_LOCK_TIMEOUT_SECONDS", 10.0),
        ),
        mapping_config_dir=Path(
            os.getenv("MAPPING_CONFIG_DIR", Path(__file__).resolve().parent.parent / "mapping" / "configs")
        ),
        default_tenant_id=os.getenv("DEFAULT_TENANT_ID", "meridian"),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()
