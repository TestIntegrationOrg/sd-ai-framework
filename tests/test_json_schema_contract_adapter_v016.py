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


def test_root_and_plain_name_anchor_refs_are_local(tmp_path: Path) -> None:
    root_ref = _snapshot(tmp_path, "root-ref", {"$ref": "#"})
    assert check_contract(root_ref, _registry()).valid

    anchor_ref = _snapshot(
        tmp_path,
        "anchor-ref",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {"identifier": {"$anchor": "identifier", "type": "string"}},
            "properties": {"id": {"$ref": "#identifier"}},
        },
    )
    assert check_contract(anchor_ref, _registry()).valid

    unresolved = _snapshot(tmp_path, "unresolved-anchor", {"$ref": "#missing"})
    codes = {item.code for item in check_contract(unresolved, _registry()).findings}
    assert "SDAI-CONTRACT-JSONSCHEMA-008" in codes
    assert "SDAI-CONTRACT-JSONSCHEMA-007" not in codes


def test_duplicate_anchor_and_dynamic_reference_fail_closed(tmp_path: Path) -> None:
    duplicate = _snapshot(
        tmp_path,
        "duplicate-anchor",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {
                "left": {"$anchor": "shared", "type": "string"},
                "right": {"$anchor": "shared", "type": "integer"},
            },
        },
    )
    assert "SDAI-CONTRACT-JSONSCHEMA-005" in {
        item.code for item in check_contract(duplicate, _registry()).findings
    }

    dynamic = _snapshot(
        tmp_path,
        "dynamic-ref",
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "$dynamicRef": "#node"},
    )
    assert "SDAI-CONTRACT-JSONSCHEMA-009" in {
        item.code for item in check_contract(dynamic, _registry()).findings
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


def test_additional_properties_schema_transition_is_breaking(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", {"type": "object"})
    after = _snapshot(
        tmp_path,
        "after",
        {"type": "object", "additionalProperties": {"type": "string"}},
    )
    codes = {item.code for item in diff_contracts(before, after, _registry()).findings}
    assert "SDAI-CONTRACT-JSONSCHEMA-DIFF-015" in codes
    assert "SDAI-CONTRACT-JSONSCHEMA-DIFF-010" in codes


def test_declaring_property_can_restrict_previously_additional_value(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", {"type": "object"})
    after = _snapshot(
        tmp_path,
        "after",
        {"type": "object", "properties": {"id": {"type": "string"}}},
    )
    result = diff_contracts(before, after, _registry())
    assert not result.compatible
    assert "SDAI-CONTRACT-JSONSCHEMA-DIFF-017" in {item.code for item in result.findings}


def test_declaring_property_is_widening_when_it_was_previously_forbidden(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", {"type": "object", "additionalProperties": False})
    after = _snapshot(
        tmp_path,
        "after",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"id": {"type": "string"}},
        },
    )
    assert diff_contracts(before, after, _registry()).compatible


def test_removing_declared_property_is_breaking_when_additional_is_forbidden(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"id": {"type": "string"}},
        },
    )
    after = _snapshot(tmp_path, "after", {"type": "object", "additionalProperties": False})
    result = diff_contracts(before, after, _registry())
    assert not result.compatible
    assert "SDAI-CONTRACT-JSONSCHEMA-DIFF-018" in {item.code for item in result.findings}


def test_composition_change_is_classified(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", {"oneOf": [{"type": "string"}, {"type": "integer"}]})
    after = _snapshot(tmp_path, "after", {"oneOf": [{"type": "string"}]})
    codes = {item.code for item in diff_contracts(before, after, _registry()).findings}
    assert "SDAI-CONTRACT-JSONSCHEMA-DIFF-016" in codes


def test_modern_ref_sibling_assertion_change_is_classified(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {"value": {"type": "string"}},
            "$ref": "#/$defs/value",
            "minLength": 1,
        },
    )
    after = _snapshot(
        tmp_path,
        "after",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {"value": {"type": "string"}},
            "$ref": "#/$defs/value",
            "minLength": 2,
        },
    )
    codes = {item.code for item in diff_contracts(before, after, _registry()).findings}
    assert "SDAI-CONTRACT-JSONSCHEMA-DIFF-016" in codes


def test_draft7_ref_siblings_are_ignored_for_compatibility(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "definitions": {"value": {"type": "string"}},
            "$ref": "#/definitions/value",
            "minLength": 1,
        },
    )
    after = _snapshot(
        tmp_path,
        "after",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "definitions": {"value": {"type": "string"}},
            "$ref": "#/definitions/value",
            "minLength": 2,
        },
    )
    assert diff_contracts(before, after, _registry()).compatible


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
