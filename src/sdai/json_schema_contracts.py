from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping, Sequence

import yaml

from sdai.contracts import (
    CompatibilityDirection,
    ContractFinding,
    ContractProvenance,
    ContractSeverity,
    ContractSnapshot,
)
from sdai.structured_contracts import (
    UniqueKeySafeLoader,
    escape_json_pointer,
    normalize_structured_json,
    resolve_local_json_pointer,
)


_DIALECTS = {
    "http://json-schema.org/draft-07/schema#": "draft-07",
    "https://json-schema.org/draft/2019-09/schema": "2019-09",
    "https://json-schema.org/draft/2019-09/schema#": "2019-09",
    "https://json-schema.org/draft/2020-12/schema": "2020-12",
    "https://json-schema.org/draft/2020-12/schema#": "2020-12",
}
_DEFAULT_DIALECT = "2020-12"


@dataclass(frozen=True, slots=True)
class _Parsed:
    schema: object | None
    dialect: str | None
    findings: tuple[ContractFinding, ...]


def _provenance(snapshot: ContractSnapshot, pointer: str | None = None) -> ContractProvenance:
    return ContractProvenance(
        source_id=snapshot.source.source_id,
        source_path=snapshot.source.path,
        source_sha256=snapshot.sha256,
        pointer=pointer,
    )


def _finding(
    snapshot: ContractSnapshot,
    code: str,
    message: str,
    *,
    pointer: str | None = None,
    severity: ContractSeverity = ContractSeverity.ERROR,
    compatibility: CompatibilityDirection = CompatibilityDirection.NONE,
) -> ContractFinding:
    return ContractFinding(
        code=code,
        severity=severity,
        message=message,
        compatibility=compatibility,
        provenance=_provenance(snapshot, pointer),
    )


def _freeze(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _scan_refs(
    snapshot: ContractSnapshot,
    root: Mapping[str, object],
    value: object,
    *,
    pointer: str = "",
) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    if isinstance(value, Mapping):
        reference = value.get("$ref")
        if reference is not None:
            ref_pointer = f"{pointer}/$ref"
            if not isinstance(reference, str):
                findings.append(_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-006", "$ref must be a string", pointer=ref_pointer))
            elif not reference.startswith("#/"):
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-JSONSCHEMA-007",
                        f"external or non-local reference is not allowed: {reference}",
                        pointer=ref_pointer,
                    )
                )
            else:
                try:
                    resolve_local_json_pointer(root, reference)
                except (KeyError, ValueError):
                    findings.append(
                        _finding(
                            snapshot,
                            "SDAI-CONTRACT-JSONSCHEMA-008",
                            f"unresolved local reference: {reference}",
                            pointer=ref_pointer,
                        )
                    )
        for key in sorted(value):
            findings.extend(
                _scan_refs(
                    snapshot,
                    root,
                    value[key],
                    pointer=f"{pointer}/{escape_json_pointer(key)}",
                )
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_scan_refs(snapshot, root, item, pointer=f"{pointer}/{index}"))
    return findings


def _parse(snapshot: ContractSnapshot) -> _Parsed:
    try:
        raw = yaml.load(snapshot.text, Loader=UniqueKeySafeLoader)
    except yaml.YAMLError:
        return _Parsed(None, None, (_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-001", "JSON Schema document is not valid YAML/JSON"),))
    try:
        schema = normalize_structured_json(raw)
    except ValueError as exc:
        return _Parsed(None, None, (_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-002", str(exc)),))
    if not isinstance(schema, (Mapping, bool)):
        return _Parsed(None, None, (_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-002", "JSON Schema root must be an object or boolean schema"),))

    findings: list[ContractFinding] = []
    dialect = _DEFAULT_DIALECT
    if isinstance(schema, Mapping):
        raw_dialect = schema.get("$schema")
        if raw_dialect is not None:
            if not isinstance(raw_dialect, str) or raw_dialect not in _DIALECTS:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-JSONSCHEMA-003",
                        f"unsupported JSON Schema dialect: {raw_dialect!r}",
                        pointer="/$schema",
                    )
                )
                dialect = None
            else:
                dialect = _DIALECTS[raw_dialect]
        findings.extend(_scan_refs(snapshot, schema, schema))
    if dialect is not None:
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-JSONSCHEMA-000",
                f"effective dialect: {dialect}",
                pointer="/$schema",
                severity=ContractSeverity.INFO,
            )
        )
    return _Parsed(
        schema,
        dialect,
        tuple(sorted(findings, key=lambda item: (item.severity.value, item.code, item.provenance.pointer or "", item.message))),
    )


def _resolve(schema: object, root: object, *, seen: frozenset[str] = frozenset()) -> object:
    current = schema
    if not isinstance(root, Mapping):
        return current
    for _ in range(32):
        if not isinstance(current, Mapping) or not isinstance(current.get("$ref"), str):
            return current
        reference = current["$ref"]
        if not reference.startswith("#/") or reference in seen:
            return current
        seen = seen | {reference}
        try:
            current = resolve_local_json_pointer(root, reference)
        except (KeyError, ValueError):
            return current
    return current


def _types(schema: Mapping[str, object]) -> frozenset[str] | None:
    value = schema.get("type")
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return frozenset(value)
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _constraint_tightened(before: Mapping[str, object], after: Mapping[str, object]) -> list[tuple[str, str]]:
    tightened: list[tuple[str, str]] = []
    minimums = (("minimum", True), ("exclusiveMinimum", True), ("minLength", True), ("minItems", True), ("minProperties", True))
    maximums = (("maximum", False), ("exclusiveMaximum", False), ("maxLength", False), ("maxItems", False), ("maxProperties", False))
    for key, increasing_breaks in (*minimums, *maximums):
        old, new = _number(before.get(key)), _number(after.get(key))
        if old is None and new is not None:
            tightened.append((key, f"constraint '{key}' was introduced"))
        elif old is not None and new is not None:
            if (increasing_breaks and new > old) or (not increasing_breaks and new < old):
                tightened.append((key, f"constraint '{key}' became more restrictive"))
    for key in ("pattern", "format"):
        old, new = before.get(key), after.get(key)
        if new is not None and old != new:
            tightened.append((key, f"constraint '{key}' changed"))
    return tightened


def _one_way_diff(
    before: object,
    after: object,
    *,
    before_root: object,
    after_root: object,
    snapshot: ContractSnapshot,
    pointer: str,
    direction: CompatibilityDirection,
) -> list[ContractFinding]:
    before = _resolve(before, before_root)
    after = _resolve(after, after_root)
    if before is False:
        return []
    if after is True:
        return []
    if before is True and after is False:
        return [_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-001", "candidate rejects instances previously accepted by an unconstrained schema", pointer=pointer, compatibility=direction)]
    if before is False and after is not False:
        return []
    if after is False:
        return [_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-001", "candidate became a rejecting boolean schema", pointer=pointer, compatibility=direction)]
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return []

    findings: list[ContractFinding] = []
    old_types, new_types = _types(before), _types(after)
    if old_types is None and new_types is not None:
        findings.append(_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-010", "type constraint was introduced", pointer=f"{pointer}/type", compatibility=direction))
    elif old_types is not None and new_types is not None and not old_types <= new_types:
        findings.append(_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-010", "type constraint removed previously accepted type(s)", pointer=f"{pointer}/type", compatibility=direction))

    old_enum, new_enum = before.get("enum"), after.get("enum")
    if new_enum is not None:
        if not isinstance(new_enum, list):
            findings.append(_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-011", "candidate enum is malformed", pointer=f"{pointer}/enum", compatibility=direction))
        elif not isinstance(old_enum, list):
            findings.append(_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-011", "enum constraint was introduced", pointer=f"{pointer}/enum", compatibility=direction))
        elif not {_freeze(item) for item in old_enum} <= {_freeze(item) for item in new_enum}:
            findings.append(_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-011", "enum removed previously accepted value(s)", pointer=f"{pointer}/enum", compatibility=direction))

    if "const" in after and ("const" not in before or _freeze(before.get("const")) != _freeze(after.get("const"))):
        findings.append(_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-012", "const constraint was introduced or changed", pointer=f"{pointer}/const", compatibility=direction))

    old_required = set(before.get("required", [])) if isinstance(before.get("required"), list) else set()
    new_required = set(after.get("required", [])) if isinstance(after.get("required"), list) else set()
    for name in sorted(new_required - old_required):
        findings.append(_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-013", f"property '{name}' became required", pointer=f"{pointer}/required", compatibility=direction))

    for key, message in _constraint_tightened(before, after):
        findings.append(_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-014", message, pointer=f"{pointer}/{key}", compatibility=direction))

    old_additional = before.get("additionalProperties", True)
    new_additional = after.get("additionalProperties", True)
    if old_additional is not False and new_additional is False:
        findings.append(_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-015", "additionalProperties became false", pointer=f"{pointer}/additionalProperties", compatibility=direction))
    elif isinstance(old_additional, Mapping) and isinstance(new_additional, Mapping):
        findings.extend(_one_way_diff(old_additional, new_additional, before_root=before_root, after_root=after_root, snapshot=snapshot, pointer=f"{pointer}/additionalProperties", direction=direction))

    old_properties = before.get("properties") if isinstance(before.get("properties"), Mapping) else {}
    new_properties = after.get("properties") if isinstance(after.get("properties"), Mapping) else {}
    for name in sorted(set(old_properties) & set(new_properties)):
        findings.extend(
            _one_way_diff(
                old_properties[name],
                new_properties[name],
                before_root=before_root,
                after_root=after_root,
                snapshot=snapshot,
                pointer=f"{pointer}/properties/{escape_json_pointer(name)}",
                direction=direction,
            )
        )

    for keyword in ("allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentSchemas"):
        if keyword in after and _freeze(before.get(keyword)) != _freeze(after.get(keyword)):
            findings.append(_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-DIFF-016", f"composition constraint '{keyword}' changed", pointer=f"{pointer}/{keyword}", compatibility=direction))
    return findings


class JSONSchemaContractAdapter:
    kind = "json-schema"

    def check(self, snapshot: ContractSnapshot) -> Sequence[ContractFinding]:
        return _parse(snapshot).findings

    def diff(
        self,
        before: ContractSnapshot,
        after: ContractSnapshot,
        direction: CompatibilityDirection,
    ) -> Sequence[ContractFinding]:
        baseline, candidate = _parse(before), _parse(after)
        parse_findings = [*baseline.findings, *candidate.findings]
        if baseline.schema is None or candidate.schema is None or any(
            item.severity is ContractSeverity.ERROR for item in parse_findings
        ):
            return tuple(parse_findings)
        if direction is CompatibilityDirection.FORWARD:
            return tuple(_one_way_diff(candidate.schema, baseline.schema, before_root=candidate.schema, after_root=baseline.schema, snapshot=before, pointer="", direction=CompatibilityDirection.FORWARD))
        if direction is CompatibilityDirection.FULL:
            return tuple([
                *_one_way_diff(baseline.schema, candidate.schema, before_root=baseline.schema, after_root=candidate.schema, snapshot=after, pointer="", direction=CompatibilityDirection.BACKWARD),
                *_one_way_diff(candidate.schema, baseline.schema, before_root=candidate.schema, after_root=baseline.schema, snapshot=before, pointer="", direction=CompatibilityDirection.FORWARD),
            ])
        return tuple(_one_way_diff(baseline.schema, candidate.schema, before_root=baseline.schema, after_root=candidate.schema, snapshot=after, pointer="", direction=CompatibilityDirection.BACKWARD))
