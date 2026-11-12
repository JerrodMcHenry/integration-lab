"""Customer-specific field mapping: CRM account -> internal customer payload."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.mapping.transforms import CompiledTransform, TransformError, compile_transform
from app.schemas.internal_customer import (
    FIELD_OWNERSHIP,
    REQUIRED_SYNC_FIELDS,
    CustomerSyncPayload,
    FieldOwner,
)

_SOURCE_PATH = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*){0,2}$")
_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_MAX_CONFIG_BYTES = 256 * 1024


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MappingIssue:
    field: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


class MappingConfigError(ValueError):
    """A mapping configuration is invalid and must not be used."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("Invalid mapping configuration: " + "; ".join(problems))
        self.problems = problems


class MappingError(ValueError):
    """A source record could not be mapped (data problem, not retryable)."""

    code = "mapping_failed"

    def __init__(self, issues: list[MappingIssue]) -> None:
        super().__init__("; ".join(f"{i.field}: {i.message}" for i in issues))
        self.issues = issues


# --------------------------------------------------------------------------- #
# Config schema
# --------------------------------------------------------------------------- #
class TransformSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class FieldRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    target: str
    required: bool = False
    transforms: list[TransformSpec | str] = Field(default_factory=list, max_length=10)

    @field_validator("source")
    @classmethod
    def _valid_source(cls, v: str) -> str:
        if not _SOURCE_PATH.match(v):
            raise ValueError("source must be a field name or dotted path (max depth 3)")
        return v


class MappingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    version: int = Field(ge=1)
    description: str | None = None
    fields: list[FieldRule] = Field(min_length=1, max_length=100)


# --------------------------------------------------------------------------- #
# Mapper
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _CompiledRule:
    source: str
    target: str
    required: bool
    transforms: tuple[CompiledTransform, ...]


class FieldMapper:
    """Validated, compiled mapping for one tenant.

    Construction validates the whole config, so an invalid mapping can never be
    used for synchronization.
    """

    def __init__(self, config: MappingConfig) -> None:
        self.config = config
        self.tenant_id = config.tenant_id
        self.version = config.version
        self._rules = self._compile(config)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FieldMapper:
        try:
            config = MappingConfig.model_validate(data)
        except ValidationError as exc:
            raise MappingConfigError(
                [f"{'.'.join(map(str, e['loc'])) or 'config'}: {e['msg']}" for e in exc.errors()]
            ) from exc
        return cls(config)

    @staticmethod
    def _compile(config: MappingConfig) -> tuple[_CompiledRule, ...]:
        problems: list[str] = []
        seen_targets: set[str] = set()
        compiled: list[_CompiledRule] = []

        if not _TENANT_ID.match(config.tenant_id):
            problems.append("tenant_id must be lowercase letters, digits or hyphens")

        for idx, rule in enumerate(config.fields):
            where = f"fields[{idx}] ({rule.target})"
            owner = FIELD_OWNERSHIP.get(rule.target)
            if owner is None:
                problems.append(f"{where}: unknown target field")
            elif owner is not FieldOwner.CRM:
                problems.append(
                    f"{where}: target is {owner.value}-owned and cannot be written by sync"
                )
            if rule.target in seen_targets:
                problems.append(f"{where}: target mapped more than once")
            seen_targets.add(rule.target)

            transforms: list[CompiledTransform] = []
            for t_idx, spec in enumerate(rule.transforms):
                name, args = (spec, {}) if isinstance(spec, str) else (spec.name, spec.args)
                try:
                    transforms.append(compile_transform(name, args))
                except KeyError:
                    problems.append(f"{where}.transforms[{t_idx}]: unknown transform '{name}'")
                except ValidationError as exc:
                    detail = ", ".join(e["msg"] for e in exc.errors())
                    problems.append(f"{where}.transforms[{t_idx}] '{name}': invalid args ({detail})")
            compiled.append(_CompiledRule(rule.source, rule.target, rule.required, tuple(transforms)))

        missing = REQUIRED_SYNC_FIELDS - seen_targets
        if missing:
            problems.append(f"required destination fields not mapped: {sorted(missing)}")

        if problems:
            raise MappingConfigError(problems)
        return tuple(compiled)

    def map(self, source: dict[str, Any]) -> CustomerSyncPayload:
        """Map a CRM record. Raises ``MappingError`` listing *all* issues found."""
        issues: list[MappingIssue] = []
        out: dict[str, Any] = {}

        for rule in self._rules:
            value = _get_path(source, rule.source)
            try:
                for t in rule.transforms:
                    value = t.apply(value)
            except TransformError as exc:
                issues.append(MappingIssue(rule.target, "transform_failed", str(exc)))
                continue
            if value in (None, ""):
                if rule.required:
                    issues.append(
                        MappingIssue(rule.target, "missing_required_field",
                                     f"source field '{rule.source}' is missing or empty")
                    )
                else:
                    # CRM owns this field: an empty source value clears the destination.
                    out[rule.target] = None
                continue
            out[rule.target] = value

        if issues:
            raise MappingError(issues)

        try:
            return CustomerSyncPayload.model_validate(out)
        except ValidationError as exc:
            # Use pydantic's message only; never echo the offending input value.
            raise MappingError(
                [MappingIssue(".".join(map(str, e["loc"])) or "record", e["type"], e["msg"])
                 for e in exc.errors()]
            ) from exc


def _get_path(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


# --------------------------------------------------------------------------- #
# Registry (file-backed for now; moves to the database with tenants in M4/M5)
# --------------------------------------------------------------------------- #
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
