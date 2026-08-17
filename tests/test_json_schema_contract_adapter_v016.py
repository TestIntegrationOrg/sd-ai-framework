from __future__ import annotations

import json
from pathlib import Path

from sdai.contracts import (
    CompatibilityDirection,
    ContractAdapterRegistry,
    ContractSource,
    check_contract,
    diff_contracts,
    load_contract_snapshot,
)
from sdai.json_schema_contracts import JSONSchemaContractAdapter


def _snapshot(root: Path, name: str, value: object):
    relative = f"contracts/{name}.json"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, sort_keys=True), encoding="utf-8", newline="\n")
    return load_contract_snapshot(root, ContractSource(source_id=name, kind="json-schema", path=relative))


def _registry() -> ContractAdapterRegistry:
    return ContractAdapterRegistry([JSONSchemaContractAdapter()])


def test_supported_dialect_is_recorded_without_invalidating_schema(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        "schema",
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"},
    )
    result = check_contract(snapshot, _registry())
    assert result.valid
    assert any(item.code == "SDAI-CONTRACT-JSONSCHEMA-000" and "2020-12" in item.message for item in result.findings)


def test_unsupported_dialect_fails_closed(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "schema", {"$schema": "https://example.invalid/schema", "type": "object"})
    result = check_contract(snapshot, _registry())
    assert not result.valid
    assert "SDAI-CONTRACT-JSONSCHEMA-003" in {item.code for item in result.findings}


def test_local_refs_are_allowed_and_remote_refs_are_rejected(tmp_path: Path) -> None:
    local = _snapshot(
        tmp_path,
        "local",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {"id": {"type": "string"}},
            "properties": {"id": {"$ref": "#/$defs/id"}},
        },
    )
    assert check_contract(local, _registry()).valid
    remote = _snapshot(
        tmp_path,
        "remote",
        {"$ref": "https://example.invalid/schemas/id.json"},
    )
    assert "SDAI-CONTRACT-JSONSCHEMA-007" in {
        item.code for item in check_contract(remote, _registry()).findings
    }


def test_type_enum_and_required_narrowing_are_breaking(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        {
            "type": "object",
            "properties": {
                "state": {"type": ["string", "null"], "enum": ["active", "inactive", None]},
            },
        },
    )
    after = _snapshot(
        tmp_path,
        "after",
        {
            "type": "object",
            "required": ["state"],
            "properties": {"state": {"type": "string", "enum": ["active"]}},
        },
    )
    codes = {item.code for item in diff_contracts(before, after, _registry()).findings}
    assert {
        "SDAI-CONTRACT-JSONSCHEMA-DIFF-010",
        "SDAI-CONTRACT-JSONSCHEMA-DIFF-011",
        "SDAI-CONTRACT-JSONSCHEMA-DIFF-013",
    } <= codes


def test_bounds_and_additional_properties_tightening_are_breaking(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        {"type": "object", "properties": {"count": {"type": "integer", "minimum": 0}}},
    )
    after = _snapshot(
        tmp_path,
        "after",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 100}},
        },
    )
    codes = {item.code for item in diff_contracts(before, after, _registry()).findings}
    assert "SDAI-CONTRACT-JSONSCHEMA-DIFF-014" in codes
    assert "SDAI-CONTRACT-JSONSCHEMA-DIFF-015" in codes


def test_composition_change_is_classified(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", {"oneOf": [{"type": "string"}, {"type": "integer"}]})
    after = _snapshot(tmp_path, "after", {"oneOf": [{"type": "string"}]})
    codes = {item.code for item in diff_contracts(before, after, _registry()).findings}
    assert "SDAI-CONTRACT-JSONSCHEMA-DIFF-016" in codes


def test_widening_is_backward_compatible_but_forward_breaking(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", {"type": "string", "enum": ["active"]})
    after = _snapshot(tmp_path, "after", {"type": ["string", "null"], "enum": ["active", None]})
    backward = diff_contracts(before, after, _registry(), CompatibilityDirection.BACKWARD)
    forward = diff_contracts(before, after, _registry(), CompatibilityDirection.FORWARD)
    full = diff_contracts(before, after, _registry(), CompatibilityDirection.FULL)
    assert backward.compatible
    assert not forward.compatible
    assert not full.compatible


def test_boolean_schema_direction_is_deterministic(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", True)
    after = _snapshot(tmp_path, "after", False)
    left = diff_contracts(before, after, _registry())
    right = diff_contracts(before, after, _registry())
    assert not left.compatible
    assert left.to_json() == right.to_json()
