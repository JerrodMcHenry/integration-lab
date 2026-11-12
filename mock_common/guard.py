"""Guard for development-only admin endpoints in the mock services.

Rules (fail closed):
- ``APP_ENV`` in development/test/local: admin routes are on unless
  ``MOCK_ADMIN_ENABLED=false``.
- ``APP_ENV`` in production/prod/staging: admin routes are never mounted.
  Setting ``MOCK_ADMIN_ENABLED=true`` there raises at startup so the
  misconfiguration can't go unnoticed.
- Any other or unknown ``APP_ENV``: off unless ``MOCK_ADMIN_ENABLED=true``.
"""

from __future__ import annotations

import os

DEV_ENVS = frozenset({"development", "dev", "test", "local"})
PRODUCTION_ENVS = frozenset({"production", "prod", "staging"})
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class UnsafeAdminConfiguration(RuntimeError):
    pass


def admin_routes_enabled(app_env: str | None = None, flag: str | None = None) -> bool:
    env = (app_env if app_env is not None else os.getenv("APP_ENV", "development")).strip().lower()
    raw = flag if flag is not None else os.getenv("MOCK_ADMIN_ENABLED")
    explicit: bool | None = None
    if raw not in (None, ""):
        value = raw.strip().lower()
        if value in _TRUE:
            explicit = True
        elif value in _FALSE:
            explicit = False
        else:
            raise UnsafeAdminConfiguration(f"MOCK_ADMIN_ENABLED has unrecognised value {raw!r}")

    if env in PRODUCTION_ENVS:
        if explicit:
            raise UnsafeAdminConfiguration(
                f"Mock admin endpoints cannot be enabled when APP_ENV={env!r}")
        return False
    if explicit is not None:
        return explicit
    return env in DEV_ENVS
