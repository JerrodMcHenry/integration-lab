"""Development tenant seed data (seed/dev_tenants.json)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.repositories.tenants import Tenant, TenantIntegration

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED_FILE = ROOT / "seed" / "dev_tenants.json"
MAPPING_DIR = ROOT / "app" / "mapping" / "configs"
_VAR = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


@dataclass(frozen=True)
class SeedTenant:
    tenant: Tenant
    integration: TenantIntegration | None
    mapping: dict[str, Any] | None


def _expand(value: str) -> str:
    return _VAR.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)


def load_seed(path: Path = DEFAULT_SEED_FILE) -> list[SeedTenant]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[SeedTenant] = []
    for t in data["tenants"]:
        integration = None
        if "integration" in t:
            fields = {k: _expand(v) for k, v in t["integration"].items()}
            integration = TenantIntegration(tenant_id=t["id"], **fields)
        mapping = None
        if t.get("mapping_file"):
            mapping = json.loads((MAPPING_DIR / t["mapping_file"]).read_text(encoding="utf-8"))
            if mapping.get("tenant_id") != t["id"]:
                raise ValueError(f"mapping file for {t['id']} declares tenant {mapping.get('tenant_id')!r}")
        out.append(SeedTenant(Tenant(t["id"], t["display_name"], t.get("status", "active")),
                              integration, mapping))
    return out
