"""Role-based access control.

Roles come from the verified access token. Endpoints depend on
*permissions*, never on role names, so the role→permission mapping can
change in one place.

Every role is scoped to the tenant in the token. There is deliberately no
cross-tenant "super admin" role in the API.
"""

from __future__ import annotations

from enum import Enum


class Permission(str, Enum):
    TENANT_READ = "tenant:read"
    HISTORY_READ = "history:read"
    SYNC_RUN = "sync:run"
    CONFIG_READ = "config:read"
    CONFIG_WRITE = "config:write"


class Role(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


_VIEWER = frozenset({Permission.TENANT_READ, Permission.HISTORY_READ})
_OPERATOR = _VIEWER | {Permission.SYNC_RUN}
_ADMIN = _OPERATOR | {Permission.CONFIG_READ, Permission.CONFIG_WRITE}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.OPERATOR: _OPERATOR,
    Role.ADMIN: _ADMIN,
}


def permissions_for(roles: frozenset[Role]) -> frozenset[Permission]:
    out: frozenset[Permission] = frozenset()
    for role in roles:
        out |= ROLE_PERMISSIONS[role]
    return out
