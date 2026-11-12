"""Shared test constants: repository paths, tenant mapping configs, secrets, mock endpoints."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAPPING_DIR = ROOT / "app" / "mapping" / "configs"
MAPPING = json.loads((MAPPING_DIR / "meridian.json").read_text())
HARBOR_MAPPING = json.loads((MAPPING_DIR / "harbor.json").read_text())

# Single-tenant in-process mocks (tests/conftest.py fixtures crm_app / cms_app).
CRM_BASE = "http://crm.test"
CMS_BASE = "http://cms.test"
CMS_KEY = "test-internal-key"

#: Per-tenant CRM/CMS credentials, referenced as env:IL_<TENANT>__<NAME>.
CONNECTION_SECRETS = {
    "IL_MERIDIAN__CRM_CLIENT_SECRET": "meridian-crm-secret",
    "IL_MERIDIAN__CMS_API_KEY": "meridian-cms-key",
    "IL_HARBOR__CRM_CLIENT_SECRET": "harbor-crm-secret",
    "IL_HARBOR__CMS_API_KEY": "harbor-cms-key",
}
#: Per-tenant webhook signing secrets.
WEBHOOK_SECRETS = {
    "IL_MERIDIAN__WEBHOOK_SECRET": "whsec_meridian_test",
    "IL_HARBOR__WEBHOOK_SECRET": "whsec_harbor_test",
}
ALL_SECRETS = {**CONNECTION_SECRETS, **WEBHOOK_SECRETS}
