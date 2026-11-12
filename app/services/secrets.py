"""Resolve per-tenant secret references.

The database stores *references*, never secret values:

    env:IL_<TENANT>__<NAME>      e.g. env:IL_MERIDIAN__CRM_CLIENT_SECRET

``<TENANT>`` is the tenant id upper-cased with ``-`` replaced by ``_``. The
double underscore separates the tenant from the secret name. ``<NAME>`` is
``[A-Z0-9]+`` groups joined by single underscores.

The reference is *parsed*, not prefix-matched. A plain prefix check lets
tenant ``a`` (prefix ``IL_A_``) read ``IL_A_B_KEY`` belonging to tenant
``a-b``. Parsing at the first ``__`` and requiring an exact tenant-segment
match plus a strict name grammar makes the mapping unambiguous: a reference
resolves for at most one tenant. Tenant ids whose segment would contain
``__`` or end in ``_`` (ids with ``--`` or a trailing ``-``) can't resolve
any secret, so they fail closed rather than colliding.

``env:`` is the only backend today. A vault/secret-manager backend can be
added behind the same interface.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from app.core.config import PRODUCTION_ENVS

_REF = re.compile(r"^env:IL_([A-Z0-9_]{1,120})$")
_NAME = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$")


class SecretResolutionError(RuntimeError):
    code = "tenant_secret_unavailable"


def tenant_segment(tenant_id: str) -> str:
    return tenant_id.upper().replace("-", "_")


def secret_env_name(tenant_id: str, name: str) -> str:
    """The env var name for ``name`` in ``tenant_id``'s namespace."""
    return f"IL_{tenant_segment(tenant_id)}__{name}"


class EnvSecretResolver:
    def __init__(self, environ: Mapping[str, str] | None = None, *, app_env: str | None = None) -> None:
        self._environ = environ if environ is not None else os.environ
        self._production = (app_env or os.getenv("APP_ENV", "development")).lower() in PRODUCTION_ENVS

    def resolve(self, tenant_id: str, ref: str) -> str:
        match = _REF.match(ref or "")
        if not match:
            raise SecretResolutionError("secret reference has an unsupported format")
        segment, sep, name = match.group(1).partition("__")
        if not sep or not _NAME.match(name):
            raise SecretResolutionError("secret reference has an unsupported format")
        if segment != tenant_segment(tenant_id):
            # Never reveal which tenant the reference pointed to.
            raise SecretResolutionError("secret reference is outside the tenant's namespace")
        env_name = f"IL_{segment}__{name}"
        value = self._environ.get(env_name)
        if not value:
            raise SecretResolutionError(f"secret {env_name} is not configured")
        if self._production and value.startswith("dev-only"):
            raise SecretResolutionError(f"secret {env_name} uses a development value in production")
        return value
