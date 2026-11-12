"""File-based mapping loader (test-only).

Production loads mappings from the database (``app.services.tenant_context.DbMappingProvider``).
Many unit tests still build mappers straight from ``app/mapping/configs/*.json``; this loader
does that with the same validation (tenant-id format, size cap, tenant match).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.mapping.mapper import FieldMapper, MappingConfigError

_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_MAX_CONFIG_BYTES = 256 * 1024


class MappingRegistry:
    def __init__(self, config_dir: Path) -> None:
        self._dir = config_dir
        self._cache: dict[str, FieldMapper] = {}

    def get(self, tenant_id: str) -> FieldMapper:
        if tenant_id in self._cache:
            return self._cache[tenant_id]
        if not _TENANT_ID.match(tenant_id):
            # Also blocks path traversal via tenant ids.
            raise MappingConfigError(["invalid tenant id"])
        path = self._dir / f"{tenant_id}.json"
        if not path.is_file():
            raise MappingConfigError([f"no mapping configured for tenant '{tenant_id}'"])
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            raise MappingConfigError(["mapping file too large"])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MappingConfigError([f"mapping file is not valid JSON (line {exc.lineno})"]) from exc
        mapper = FieldMapper.from_dict(data)
        if mapper.tenant_id != tenant_id:
            raise MappingConfigError(["tenant_id in file does not match file name"])
        self._cache[tenant_id] = mapper
        return mapper
