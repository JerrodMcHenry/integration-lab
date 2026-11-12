"""Whitelisted value transforms for field mappings.

Mapping configs refer to transforms *by name* only. Each transform has a typed
argument model that is validated when the config loads. Nothing in a config is
ever evaluated or imported, so a mapping file cannot execute code.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TransformError(ValueError):
    """A value could not be transformed. Messages never include the value itself."""


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _DecimalArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    places: int = Field(default=2, ge=0, le=6)


class _TruncateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_length: int = Field(ge=1, le=10_000)


class _ValueMapArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(min_length=1, max_length=500)
    default: str | None = None
    #: When True, an unmapped value with no default fails the record.
    strict: bool = False


class _DefaultArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | int | float | bool


@dataclass(frozen=True)
class TransformDef:
    args_model: type[BaseModel]
    fn: Callable[[Any, Any], Any]
    #: Most transforms pass ``None`` through untouched; ``default`` handles it.
    handles_none: bool = False


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TransformError(f"{name} expects text, got {type(value).__name__}")
    return value


def _to_decimal(value: Any, args: _DecimalArgs) -> Decimal:
    if isinstance(value, bool):
        raise TransformError("to_decimal does not accept booleans")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise TransformError("to_decimal could not parse a number") from exc
    if not d.is_finite():
        raise TransformError("to_decimal requires a finite number")
    return d.quantize(Decimal(1).scaleb(-args.places), rounding=ROUND_HALF_UP)


def _iso_datetime(value: Any, _args: _NoArgs) -> datetime:
    text = _require_str(value, "iso_datetime")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TransformError("iso_datetime could not parse an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise TransformError("iso_datetime requires a timezone offset")
    return parsed


def _value_map(value: Any, args: _ValueMapArgs) -> str:
    key = _require_str(value, "value_map")
    if key in args.values:
        return args.values[key]
    if args.default is not None:
        return args.default
    if args.strict:
        raise TransformError("value_map has no mapping for this value")
    return key


REGISTRY: dict[str, TransformDef] = {
    "strip": TransformDef(_NoArgs, lambda v, _a: _require_str(v, "strip").strip()),
    "collapse_whitespace": TransformDef(
        _NoArgs, lambda v, _a: re.sub(r"\s+", " ", _require_str(v, "collapse_whitespace")).strip()
    ),
    "upper": TransformDef(_NoArgs, lambda v, _a: _require_str(v, "upper").upper()),
    "lower": TransformDef(_NoArgs, lambda v, _a: _require_str(v, "lower").lower()),
    "title": TransformDef(_NoArgs, lambda v, _a: _require_str(v, "title").title()),
    "truncate": TransformDef(_TruncateArgs, lambda v, a: _require_str(v, "truncate")[: a.max_length]),
    "to_decimal": TransformDef(_DecimalArgs, _to_decimal),
    "iso_datetime": TransformDef(_NoArgs, _iso_datetime),
    "value_map": TransformDef(_ValueMapArgs, _value_map),
    "default": TransformDef(_DefaultArgs, lambda v, a: a.value if v in (None, "") else v, handles_none=True),
}


@dataclass(frozen=True)
class CompiledTransform:
    name: str
    definition: TransformDef
    args: BaseModel

    def apply(self, value: Any) -> Any:
        if value is None and not self.definition.handles_none:
            return None
        return self.definition.fn(value, self.args)


def compile_transform(name: str, args: dict[str, Any]) -> CompiledTransform:
    """Resolve a transform by name and validate its arguments.

    Raises ``KeyError`` for unknown names and ``pydantic.ValidationError`` for
    bad arguments. The config loader turns both into a ``MappingConfigError``.
    """
    definition = REGISTRY[name]
    return CompiledTransform(name, definition, definition.args_model.model_validate(args))
