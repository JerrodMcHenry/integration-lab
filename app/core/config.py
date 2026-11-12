"""Environment-based configuration.

All settings come from environment variables (optionally loaded from a local
``.env`` file). Defaults are safe for local development only. In a production
environment, startup fails if any dev-default secret is still in use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

from app.integrations.retry import MAX_RETRIES_CEILING

PRODUCTION_ENVS = frozenset({"production", "prod", "staging"})

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
class HttpPolicy:
    """Platform-wide timeout/retry policy for one external system.

    Connection details (URLs, client ids, secrets) are per tenant, in
    ``tenant_integrations``; ``TenantConnectionProvider`` combines the two into
    ``CRMSettings`` / ``InternalSystemSettings`` for each tenant's clients.
    """

    timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    env_prefix: str = "HTTP"

    def __post_init__(self) -> None:
        _validate_http(self.env_prefix, self.timeout_seconds, self.max_retries,
                       self.backoff_base_seconds, self.backoff_max_seconds)


@dataclass(frozen=True)
class CRMSettings:
    """One tenant's CRM connection (built per tenant; never loaded from global env)."""

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
    """One tenant's internal CMS connection (built per tenant; never loaded from global env)."""

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
    #: SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@host:5432/db. Required.
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


DEV_OIDC_ISSUER = "http://localhost:8090"


@dataclass(frozen=True)
class OIDCSettings:
    #: Exact ``iss`` value expected in access tokens.
    issuer: str
    #: Required ``aud`` value (this API's identifier at the IdP).
    audience: str
    #: JWKS location. ``None`` means discover it from ``{issuer}/.well-known/openid-configuration``.
    jwks_url: str | None = None
    algorithms: tuple[str, ...] = ("RS256",)
    leeway_seconds: int = 30
    #: RFC 9068 JWT access tokens carry ``typ: at+jwt``. Rejecting other types stops
    #: ID tokens (or other JWTs from the same IdP) being replayed as access tokens.
    required_token_type: str | None = "at+jwt"
    tenant_claim: str = "tenant_id"
    roles_claim: str = "roles"
    jwks_cache_seconds: int = 600
    #: Reject tokens whose lifetime (exp - iat) exceeds this, even if correctly signed.
    max_token_lifetime_seconds: int = 3600

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience:
            raise ValueError("OIDC_ISSUER and OIDC_AUDIENCE are required")
        allowed = {"RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "EdDSA"}
        if not self.algorithms or not set(self.algorithms) <= allowed:
            # Symmetric (HS*) and "none" are never accepted: they enable key-confusion attacks.
            raise ValueError(f"OIDC_ALGORITHMS must be a subset of {sorted(allowed)}")
        if not 0 <= self.leeway_seconds <= 120:
            raise ValueError("OIDC_LEEWAY_SECONDS must be between 0 and 120")
        if not 60 <= self.max_token_lifetime_seconds <= 86400:
            raise ValueError("OIDC_MAX_TOKEN_LIFETIME_SECONDS must be between 60 and 86400")


@dataclass(frozen=True)
class WebhookSettings:
    #: Max |now - signed timestamp|. Older (or future-dated) deliveries are rejected as replays.
    tolerance_seconds: int = 300
    max_body_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if not 30 <= self.tolerance_seconds <= 3600:
            raise ValueError("WEBHOOK_TOLERANCE_SECONDS must be between 30 and 3600")
        if not 1024 <= self.max_body_bytes <= 1024 * 1024:
            raise ValueError("WEBHOOK_MAX_BODY_BYTES must be between 1 KiB and 1 MiB")


@dataclass(frozen=True)
class WorkerSettings:
    poll_interval_seconds: float = 1.0
    batch_size: int = 10
    #: How long a claimed event is reserved. None = derived from the worst-case sync duration.
    lease_seconds: float | None = None
    #: Max claims per event. Bounds crash loops: an event whose processing keeps
    #: killing workers is marked failed instead of being reclaimed forever.
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if not 0.05 <= self.poll_interval_seconds <= 60:
            raise ValueError("WORKER_POLL_INTERVAL_SECONDS must be between 0.05 and 60")
        if not 1 <= self.batch_size <= 100:
            raise ValueError("WORKER_BATCH_SIZE must be between 1 and 100")
        if self.lease_seconds is not None and not 1 <= self.lease_seconds <= 3600:
            raise ValueError("WORKER_LEASE_SECONDS must be between 1 and 3600")
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("WORKER_MAX_ATTEMPTS must be between 1 and 20")


@dataclass(frozen=True)
class RetrySettings:
    #: Delay before retry n is drawn from [d/2, d] where d = min(max_delay, base * 2^(n-1)).
    base_delay_seconds: float = 5.0
    max_delay_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not 0.1 <= self.base_delay_seconds <= 600:
            raise ValueError("RETRY_BASE_DELAY_SECONDS must be between 0.1 and 600")
        if not self.base_delay_seconds <= self.max_delay_seconds <= 86400:
            raise ValueError("RETRY_MAX_DELAY_SECONDS must be >= the base delay and <= 86400")


@dataclass(frozen=True)
class RetentionSettings:
    #: Destructive cleanup is OFF unless explicitly enabled; even then the CLI needs --execute.
    enabled: bool = False
    succeeded_event_days: int = 30
    ignored_event_days: int = 7
    sync_record_days: int = 90
    batch_size: int = 1000

    def __post_init__(self) -> None:
        for name in ("succeeded_event_days", "ignored_event_days", "sync_record_days"):
            if not 1 <= getattr(self, name) <= 3650:
                raise ValueError(f"RETENTION_{name.upper()} must be between 1 and 3650")
        if not 10 <= self.batch_size <= 10_000:
            raise ValueError("RETENTION_BATCH_SIZE must be between 10 and 10000")


@dataclass(frozen=True)
class Settings:
    environment: str
    #: Timeout/retry policy only; connection details are per tenant.
    crm: HttpPolicy
    internal: HttpPolicy
    database: DatabaseSettings
    oidc: OIDCSettings
    webhooks: WebhookSettings
    worker: WorkerSettings
    retry: RetrySettings
    retention: RetentionSettings
    #: Bearer token for the platform-wide Prometheus endpoint. Unset = endpoint disabled.
    metrics_token: str | None

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in PRODUCTION_ENVS

    def __post_init__(self) -> None:
        if not self.database.url:
            raise ConfigurationError("DATABASE_URL is required: IntegrationLab needs PostgreSQL in every environment")
        if self.is_production:
            problems = []
            if not self.oidc.issuer.startswith("https://"):
                problems.append("OIDC_ISSUER must use https in production")
            if self.oidc.issuer.rstrip("/") == DEV_OIDC_ISSUER:
                problems.append("OIDC_ISSUER points at the development mock IdP")
            if self.metrics_token is not None and len(self.metrics_token) < 32:
                problems.append("METRICS_TOKEN must be at least 32 characters in production")
            if self.oidc.jwks_url and not self.oidc.jwks_url.startswith("https://"):
                problems.append("OIDC_JWKS_URL must use https in production")
            if problems:
                raise ConfigurationError("Unsafe production configuration: " + "; ".join(problems))


# --------------------------------------------------------------------------- #
# Derived timing bounds (used for the lock backstop, worker lease and write deadline)
# --------------------------------------------------------------------------- #
def worst_case_sync_seconds(crm: HttpPolicy, internal: HttpPolicy) -> float:
    """Upper bound on how long one sync can hold its lock.

    CRM: token fetch + account fetch with retries. CMS: up to 3 calls with
    retries (lookup, create or conflict re-lookup, update).
    """
    def call(timeout: float, retries: int, cap: float) -> float:
        return (retries + 1) * timeout + retries * cap

    crm_s = crm.timeout_seconds + call(crm.timeout_seconds, crm.max_retries, crm.backoff_max_seconds)
    cms_s = 3 * call(internal.timeout_seconds, internal.max_retries, internal.backoff_max_seconds)
    return crm_s + cms_s


def destination_write_margin(settings: Settings) -> float:
    """Worst-case duration of ONE destination call (timeouts, in-client retries, backoff)."""
    c = settings.internal
    return (c.max_retries + 1) * c.timeout_seconds + c.max_retries * c.backoff_max_seconds


def load_settings() -> Settings:
    """Build settings from the current environment (uncached)."""
    load_dotenv()
    return Settings(
        environment=os.getenv("APP_ENV", "development"),
        crm=HttpPolicy(
            timeout_seconds=_env_float("CRM_TIMEOUT_SECONDS", 5.0),
            max_retries=_env_int("CRM_MAX_RETRIES", 2),
            backoff_base_seconds=_env_float("CRM_BACKOFF_BASE_SECONDS", 0.5),
            backoff_max_seconds=_env_float("CRM_BACKOFF_MAX_SECONDS", 4.0),
            env_prefix="CRM",
        ),
        internal=HttpPolicy(
            timeout_seconds=_env_float("INTERNAL_API_TIMEOUT_SECONDS", 5.0),
            max_retries=_env_int("INTERNAL_API_MAX_RETRIES", 2),
            backoff_base_seconds=_env_float("INTERNAL_API_BACKOFF_BASE_SECONDS", 0.5),
            backoff_max_seconds=_env_float("INTERNAL_API_BACKOFF_MAX_SECONDS", 4.0),
            env_prefix="INTERNAL_API",
        ),
        database=DatabaseSettings(
            url=os.getenv("DATABASE_URL") or None,
            pool_size=_env_int("DB_POOL_SIZE", 10),
            max_overflow=_env_int("DB_MAX_OVERFLOW", 10),
            pool_timeout_seconds=_env_float("DB_POOL_TIMEOUT_SECONDS", 5.0),
            connect_timeout_seconds=_env_int("DB_CONNECT_TIMEOUT_SECONDS", 3),
            lock_timeout_seconds=_env_float("SYNC_LOCK_TIMEOUT_SECONDS", 10.0),
        ),
        oidc=OIDCSettings(
            issuer=os.getenv("OIDC_ISSUER", DEV_OIDC_ISSUER).rstrip("/"),
            audience=os.getenv("OIDC_AUDIENCE", "integrationlab-api"),
            jwks_url=os.getenv("OIDC_JWKS_URL") or None,
            algorithms=tuple(a.strip() for a in os.getenv("OIDC_ALGORITHMS", "RS256").split(",") if a.strip()),
            leeway_seconds=_env_int("OIDC_LEEWAY_SECONDS", 30),
            required_token_type=os.getenv("OIDC_REQUIRED_TOKEN_TYPE", "at+jwt") or None,
            tenant_claim=os.getenv("OIDC_TENANT_CLAIM", "tenant_id"),
            roles_claim=os.getenv("OIDC_ROLES_CLAIM", "roles"),
            jwks_cache_seconds=_env_int("OIDC_JWKS_CACHE_SECONDS", 600),
            max_token_lifetime_seconds=_env_int("OIDC_MAX_TOKEN_LIFETIME_SECONDS", 3600),
        ),
        webhooks=WebhookSettings(
            tolerance_seconds=_env_int("WEBHOOK_TOLERANCE_SECONDS", 300),
            max_body_bytes=_env_int("WEBHOOK_MAX_BODY_BYTES", 64 * 1024),
        ),
        worker=WorkerSettings(
            poll_interval_seconds=_env_float("WORKER_POLL_INTERVAL_SECONDS", 1.0),
            batch_size=_env_int("WORKER_BATCH_SIZE", 10),
            lease_seconds=_env_float("WORKER_LEASE_SECONDS", 0) or None,
            max_attempts=_env_int("WORKER_MAX_ATTEMPTS", 5),
        ),
        retry=RetrySettings(
            base_delay_seconds=_env_float("RETRY_BASE_DELAY_SECONDS", 5.0),
            max_delay_seconds=_env_float("RETRY_MAX_DELAY_SECONDS", 300.0),
        ),
        retention=RetentionSettings(
            enabled=os.getenv("RETENTION_ENABLED", "false").strip().lower() in ("1", "true", "yes"),
            succeeded_event_days=_env_int("RETENTION_SUCCEEDED_EVENT_DAYS", 30),
            ignored_event_days=_env_int("RETENTION_IGNORED_EVENT_DAYS", 7),
            sync_record_days=_env_int("RETENTION_SYNC_RECORD_DAYS", 90),
            batch_size=_env_int("RETENTION_BATCH_SIZE", 1000),
        ),
        metrics_token=os.getenv("METRICS_TOKEN") or None,
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()
