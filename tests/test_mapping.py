"""Field mapping: config validation, transforms, and runtime data errors."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.mapping.mapper import FieldMapper, MappingConfigError, MappingError
from tests.support.mappings import MappingRegistry
from tests.support.data import MAPPING_DIR

BASE_CONFIG = {
    "tenant_id": "acme",
    "version": 1,
    "fields": [
        {"source": "Id", "target": "external_crm_id", "required": True},
        {"source": "Name", "target": "legal_name", "required": True},
        {"source": "OwnerName", "target": "relationship_manager", "required": True},
    ],
}

SOURCE = {
    "Id": "SF-1001",
    "Name": "  Acme   Capital ",
    "Industry": "Financial Services",
    "AnnualRevenue": 2500000,
    "OwnerName": "Sarah Johnson",
    "LastModifiedDate": "2026-11-01T12:00:00Z",
}


def config_with(*extra_fields, **top):
    cfg = copy.deepcopy(BASE_CONFIG)
    cfg["fields"].extend(extra_fields)
    cfg.update(top)
    return cfg


def meridian() -> FieldMapper:
    return MappingRegistry(MAPPING_DIR).get("meridian")


# --- Shipped Meridian configuration ---------------------------------------- #
def test_meridian_mapping_transforms_salesforce_account():
    payload = meridian().map(SOURCE)
    assert payload.external_crm_id == "SF-1001"
    assert payload.legal_name == "Acme Capital"
    assert payload.industry_segment == "FINANCIAL_SERVICES"
    assert payload.annual_revenue_usd == Decimal("2500000.00")
    assert payload.crm_last_modified_at == datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def test_meridian_unknown_industry_falls_back_to_default():
    assert meridian().map({**SOURCE, "Industry": "Aerospace"}).industry_segment == "OTHER"


def test_customer_specific_mappings_differ_per_tenant():
    acme = FieldMapper.from_dict(config_with(
        {"source": "Industry", "target": "industry_segment", "transforms": ["upper"]}))
    assert acme.map(SOURCE).industry_segment == "FINANCIAL SERVICES"
    assert meridian().map(SOURCE).industry_segment == "FINANCIAL_SERVICES"


def test_nested_source_path():
    mapper = FieldMapper.from_dict({
        **BASE_CONFIG,
        "fields": [
            {"source": "Id", "target": "external_crm_id", "required": True},
            {"source": "Name", "target": "legal_name", "required": True},
            {"source": "Owner.Name", "target": "relationship_manager", "required": True},
        ],
    })
    assert mapper.map({"Id": "1", "Name": "X", "Owner": {"Name": "Pat"}}).relationship_manager == "Pat"


# --- Invalid mapping configurations (rejected at load time) ---------------- #
@pytest.mark.parametrize(
    "cfg, expected",
    [
        (config_with({"source": "Industry", "target": "industry_segment", "transforms": ["exec"]}),
         "unknown transform 'exec'"),
        (config_with({"source": "Industry", "target": "industry_segment",
                      "transforms": ["__import__('os').system('id')"]}),
         "unknown transform"),
        (config_with({"source": "Industry", "target": "not_a_field"}), "unknown target field"),
        (config_with({"source": "Rating", "target": "risk_classification"}),
         "internal-owned"),
        (config_with({"source": "Stage", "target": "onboarding_status"}),
         "internal-owned"),
        (config_with({"source": "Id", "target": "customer_id"}), "system-owned"),
        (config_with({"source": "Name", "target": "legal_name"}), "mapped more than once"),
        (config_with({"source": "AnnualRevenue", "target": "annual_revenue_usd",
                      "transforms": [{"name": "to_decimal", "args": {"places": 99}}]}),
         "invalid args"),
        (config_with({"source": "AnnualRevenue", "target": "annual_revenue_usd",
                      "transforms": [{"name": "to_decimal", "args": {"code": "1+1"}}]}),
         "invalid args"),
        (config_with({"source": "Name; DROP TABLE", "target": "industry_segment"}), "source"),
        (config_with({"source": "A.B.C.D", "target": "industry_segment"}), "source"),
        (config_with(tenant_id="../etc"), "tenant_id"),
        ({**BASE_CONFIG, "fields": BASE_CONFIG["fields"][:2]}, "required destination fields not mapped"),
        ({**BASE_CONFIG, "fields": []}, "fields"),
        ({**BASE_CONFIG, "script": "print(1)"}, "Extra inputs are not permitted"),
        ({**BASE_CONFIG, "version": 0}, "version"),
    ],
)
def test_invalid_mapping_configs_are_rejected(cfg, expected):
    with pytest.raises(MappingConfigError) as exc_info:
        FieldMapper.from_dict(cfg)
    assert expected in str(exc_info.value)


def test_all_config_problems_reported_together():
    cfg = config_with(
        {"source": "Rating", "target": "risk_classification"},
        {"source": "X", "target": "bogus"},
        {"source": "Industry", "target": "industry_segment", "transforms": ["nope"]},
    )
    with pytest.raises(MappingConfigError) as exc_info:
        FieldMapper.from_dict(cfg)
    assert len(exc_info.value.problems) == 3


def test_registry_rejects_path_traversal_and_missing_tenants(tmp_path):
    registry = MappingRegistry(tmp_path)
    for bad in ("../meridian", "Meridian", "a/b", ""):
        with pytest.raises(MappingConfigError):
            registry.get(bad)
    with pytest.raises(MappingConfigError, match="no mapping configured"):
        registry.get("unknown")


def test_registry_rejects_invalid_json_and_mismatched_tenant(tmp_path):
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "other.json").write_text(json.dumps(BASE_CONFIG))  # tenant_id "acme"
    registry = MappingRegistry(tmp_path)
    with pytest.raises(MappingConfigError, match="not valid JSON"):
        registry.get("broken")
    with pytest.raises(MappingConfigError, match="does not match"):
        registry.get("other")


# --- Runtime data problems -------------------------------------------------- #
@pytest.mark.parametrize("missing", ["Name", "OwnerName", "Id"])
def test_missing_required_source_field(missing):
    source = {k: v for k, v in SOURCE.items() if k != missing}
    with pytest.raises(MappingError) as exc_info:
        meridian().map(source)
    assert [i.code for i in exc_info.value.issues] == ["missing_required_field"]


def test_blank_required_field_is_missing():
    with pytest.raises(MappingError) as exc_info:
        meridian().map({**SOURCE, "Name": "   "})
    assert exc_info.value.issues[0].field == "legal_name"


def test_multiple_data_issues_reported_together():
    with pytest.raises(MappingError) as exc_info:
        meridian().map({**SOURCE, "Name": None, "AnnualRevenue": "lots", "LastModifiedDate": "yesterday"})
    fields = {i.field for i in exc_info.value.issues}
    assert fields == {"legal_name", "annual_revenue_usd", "crm_last_modified_at"}


def test_negative_revenue_rejected_by_destination_schema():
    with pytest.raises(MappingError) as exc_info:
        meridian().map({**SOURCE, "AnnualRevenue": -5})
    assert exc_info.value.issues[0].field == "annual_revenue_usd"


def test_naive_timestamp_rejected():
    with pytest.raises(MappingError):
        meridian().map({**SOURCE, "LastModifiedDate": "2026-11-01T12:00:00"})


def test_optional_empty_field_maps_to_none_to_clear_destination():
    payload = meridian().map({**SOURCE, "Industry": None})
    dumped = payload.model_dump(exclude_unset=True)
    assert "industry_segment" in dumped and dumped["industry_segment"] is None


def test_mapping_errors_never_echo_source_values():
    secret = "Top Secret Holdings Ltd 4111111111111111"
    with pytest.raises(MappingError) as exc_info:
        meridian().map({**SOURCE, "AnnualRevenue": secret, "Name": None})
    text = str(exc_info.value) + json.dumps([i.as_dict() for i in exc_info.value.issues])
    assert secret not in text
    assert "4111" not in text


def test_value_map_strict_mode():
    mapper = FieldMapper.from_dict(config_with({
        "source": "Industry", "target": "industry_segment",
        "transforms": [{"name": "value_map", "args": {"values": {"Banking": "BANKING"}, "strict": True}}],
    }))
    with pytest.raises(MappingError, match="no mapping"):
        mapper.map(SOURCE)
