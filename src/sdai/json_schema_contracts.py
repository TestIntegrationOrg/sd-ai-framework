from __future__ import annotations

from dataclasses import dataclass
import json
import re
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
_ANCHOR_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]*$")
_REF_SIBLING_NON_ASSERTIONS = frozenset(
    {
        "$anchor",
        "$comment",
        "$defs",
        "$id",
        "$schema",
        "default",
        "definitions",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)


@dataclass(frozen=True, slots=True)
class _ResolutionContext:
    root: object
    dialect: str
    anchors: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _Parsed:
    schema: object | None
    dialect: str | None
    context: _ResolutionContext | None
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


def _collect_anchors(
    snapshot: ContractSnapshot,
    value: object,
    *,
    dialect: str,
    pointer: str = "",
    anchors: dict[str, object] | None = None,
) -> tuple[dict[str, object], list[ContractFinding]]:
    if anchors is None:
        anchors = {}
    findings: list[ContractFinding] = []
    if isinstance(value, Mapping):
        anchor = value.get("$anchor")
        if anchor is not None and dialect in {"2019-09", "2020-12"}:
            anchor_pointer = f"{pointer}/$anchor"
            if not isinstance(anchor, str) or _ANCHOR_PATTERN.fullmatch(anchor) is None:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-JSONSCHEMA-005",
                        "$anchor must be a valid plain-name fragment",
                        pointer=anchor_pointer,
                    )
                )
            else:
                reference = f"#{anchor}"
                if reference in anchors:
                    findings.append(
                        _finding(
                            snapshot,
                            "SDAI-CONTRACT-JSONSCHEMA-005",
                            f"duplicate local anchor: {reference}",
                            pointer=anchor_pointer,
                        )
                    )
                else:
                    anchors[reference] = value

        for unsupported in ("$dynamicRef", "$recursiveRef"):
            if unsupported in value:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-JSONSCHEMA-009",
                        f"{unsupported} is not supported by deterministic local reference analysis",
                        pointer=f"{pointer}/{escape_json_pointer(unsupported)}",
                    )
                )

        for key in sorted(value):
            _, child_findings = _collect_anchors(
                snapshot,
                value[key],
                dialect=dialect,
                pointer=f"{pointer}/{escape_json_pointer(key)}",
                anchors=anchors,
            )
            findings.extend(child_findings)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _, child_findings = _collect_anchors(
                snapshot,
                item,
                dialect=dialect,
                pointer=f"{pointer}/{index}",
                anchors=anchors,
            )
            findings.extend(child_findings)
    return anchors, findings


def _resolve_reference(context: _ResolutionContext, reference: str) -> object:
    if reference == "#":
        return context.root
    if reference.startswith("#/"):
        if not isinstance(context.root, Mapping):
            raise KeyError(reference)
        return resolve_local_json_pointer(context.root, reference)
    if reference.startswith("#") and reference in context.anchors:
        return context.anchors[reference]
    raise KeyError(reference)


def _scan_refs(
    snapshot: ContractSnapshot,
    context: _ResolutionContext,
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
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-JSONSCHEMA-006",
                        "$ref must be a string",
                        pointer=ref_pointer,
                    )
                )
            elif not reference.startswith("#"):
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-JSONSCHEMA-007",
                        f"external reference is not allowed: {reference}",
                        pointer=ref_pointer,
                    )
                )
            else:
                try:
                    _resolve_reference(context, reference)
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
                    context,
                    value[key],
                    pointer=f"{pointer}/{escape_json_pointer(key)}",
                )
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_scan_refs(snapshot, context, item, pointer=f"{pointer}/{index}"))
    return findings


def _parse(snapshot: ContractSnapshot) -> _Parsed:
    try:
        raw = yaml.load(snapshot.text, Loader=UniqueKeySafeLoader)
    except yaml.YAMLError:
        return _Parsed(
            None,
            None,
            None,
            (
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-JSONSCHEMA-001",
                    "JSON Schema document is not valid YAML/JSON",
                ),
            ),
        )
    try:
        schema = normalize_structured_json(raw)
    except ValueError as exc:
        return _Parsed(None, None, None, (_finding(snapshot, "SDAI-CONTRACT-JSONSCHEMA-002", str(exc)),))
    if not isinstance(schema, (Mapping, bool)):
        return _Parsed(
            None,
            None,
            None,
            (
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-JSONSCHEMA-002",
                    "JSON Schema root must be an object or boolean schema",
                ),
            ),
        )

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

    context: _ResolutionContext | None = None
    if dialect is not None:
        anchors: dict[str, object] = {}
        if isinstance(schema, Mapping):
            anchors, anchor_findings = _collect_anchors(snapshot, schema, dialect=dialect)
            findings.extend(anchor_findings)
        context = _ResolutionContext(root=schema, dialect=dialect, anchors=anchors)
        findings.extend(_scan_refs(snapshot, context, schema))
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
        context,
        tuple(
            sorted(
                findings,
                key=lambda item: (
                    item.severity.value,
                    item.code,
                    item.provenance.pointer or "",
                    item.message,
                ),
            )
        ),
    )


def _resolve(
    schema: object,
    context: _ResolutionContext,
    *,
    seen: frozenset[str] = frozenset(),
) -> object:
    current = schema
    for _ in range(32):
        if not isinstance(current, Mapping) or not isinstance(current.get("$ref"), str):
            return current
        reference = current["$ref"]
        if not reference.startswith("#") or reference in seen:
            return current
        seen = seen | {reference}
        try:
            target = _resolve_reference(context, reference)
        except (KeyError, ValueError):
            return current

        if context.dialect in {"2019-09", "2020-12"}:
            siblings = {
                key: value
                for key, value in current.items()
                if key != "$ref" and key not in _REF_SIBLING_NON_ASSERTIONS
            }
            if siblings:
                return {"allOf": [target, siblings]}
        current = target
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
    minimums = (
        ("minimum", True),
        ("exclusiveMinimum", True),
        ("minLength", True),
        ("minItems", True),
        ("minProperties", True),
    )
    maximums = (
        ("maximum", False),
        ("exclusiveMaximum", False),
        ("maxLength", False),
        ("maxItems", False),
        ("maxProperties", False),
    )
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
    before_context: _ResolutionContext,
    after_context: _ResolutionContext,
    snapshot: ContractSnapshot,
    pointer: str,
    direction: CompatibilityDirection,
) -> list[ContractFinding]:
    before = _resolve(before, before_context)
    after = _resolve(after, after_context)
    if before is False:
        return []
    if after is True:
        return []
    if after is False:
        message = (
            "candidate rejects instances previously accepted by an unconstrained schema"
            if before is True
            else "candidate became a rejecting boolean schema"
        )
        return [
            _finding(
                snapshot,
                "SDAI-CONTRACT-JSONSCHEMA-DIFF-001",
                message,
                pointer=pointer,
                compatibility=direction,
            )
        ]
    if before is True:
        before = {}
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return []

    findings: list[ContractFinding] = []
    old_types, new_types = _types(before), _types(after)
    if old_types is None and new_types is not None:
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-JSONSCHEMA-DIFF-010",
                "type constraint was introduced",
                pointer=f"{pointer}/type",
                compatibility=direction,
            )
        )
    elif old_types is not None and new_types is not None and not old_types <= new_types:
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-JSONSCHEMA-DIFF-010",
                "type constraint removed previously accepted type(s)",
                pointer=f"{pointer}/type",
                compatibility=direction,
            )
        )

    old_enum, new_enum = before.get("enum"), after.get("enum")
    if new_enum is not None:
        if not isinstance(new_enum, list):
            findings.append(
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-JSONSCHEMA-DIFF-011",
                    "candidate enum is malformed",
                    pointer=f"{pointer}/enum",
                    compatibility=direction,
                )
            )
        elif not isinstance(old_enum, list):
            findings.append(
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-JSONSCHEMA-DIFF-011",
                    "enum constraint was introduced",
                    pointer=f"{pointer}/enum",
                    compatibility=direction,
                )
            )
        elif not {_freeze(item) for item in old_enum} <= {_freeze(item) for item in new_enum}:
            findings.append(
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-JSONSCHEMA-DIFF-011",
                    "enum removed previously accepted value(s)",
                    pointer=f"{pointer}/enum",
                    compatibility=direction,
                )
            )

    if "const" in after and ("const" not in before or _freeze(before.get("const")) != _freeze(after.get("const"))):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-JSONSCHEMA-DIFF-012",
                "const constraint was introduced or changed",
                pointer=f"{pointer}/const",
                compatibility=direction,
            )
        )

    old_required = set(before.get("required", [])) if isinstance(before.get("required"), list) else set()
    new_required = set(after.get("required", [])) if isinstance(after.get("required"), list) else set()
    for name in sorted(new_required - old_required):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-JSONSCHEMA-DIFF-013",
                f"property '{name}' became required",
                pointer=f"{pointer}/required",
                compatibility=direction,
            )
        )

    for key, message in _constraint_tightened(before, after):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-JSONSCHEMA-DIFF-014",
                message,
                pointer=f"{pointer}/{key}",
                compatibility=direction,
            )
        )

    old_additional = before.get("additionalProperties", True)
    new_additional = after.get("additionalProperties", True)
    if isinstance(old_additional, (Mapping, bool)) and isinstance(new_additional, (Mapping, bool)):
        additional_findings = _one_way_diff(
            old_additional,
            new_additional,
            before_context=before_context,
            after_context=after_context,
            snapshot=snapshot,
            pointer=f"{pointer}/additionalProperties",
            direction=direction,
        )
        if additional_findings:
            findings.append(
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-JSONSCHEMA-DIFF-015",
                    "additionalProperties became more restrictive",
                    pointer=f"{pointer}/additionalProperties",
                    compatibility=direction,
                )
            )
            findings.extend(additional_findings)

    old_properties = before.get("properties") if isinstance(before.get("properties"), Mapping) else {}
    new_properties = after.get("properties") if isinstance(after.get("properties"), Mapping) else {}
    old_names, new_names = set(old_properties), set(new_properties)

    for name in sorted(old_names & new_names):
        findings.extend(
            _one_way_diff(
                old_properties[name],
                new_properties[name],
                before_context=before_context,
                after_context=after_context,
                snapshot=snapshot,
                pointer=f"{pointer}/properties/{escape_json_pointer(name)}",
                direction=direction,
            )
        )

    if isinstance(old_additional, (Mapping, bool)):
        for name in sorted(new_names - old_names):
            property_findings = _one_way_diff(
                old_additional,
                new_properties[name],
                before_context=before_context,
                after_context=after_context,
                snapshot=snapshot,
                pointer=f"{pointer}/properties/{escape_json_pointer(name)}",
                direction=direction,
            )
            if property_findings:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-JSONSCHEMA-DIFF-017",
                        f"newly declared property '{name}' restricts values previously allowed as additional properties",
                        pointer=f"{pointer}/properties/{escape_json_pointer(name)}",
                        compatibility=direction,
                    )
                )
                findings.extend(property_findings)

    if isinstance(new_additional, (Mapping, bool)):
        for name in sorted(old_names - new_names):
            property_findings = _one_way_diff(
                old_properties[name],
                new_additional,
                before_context=before_context,
                after_context=after_context,
                snapshot=snapshot,
                pointer=f"{pointer}/properties/{escape_json_pointer(name)}",
                direction=direction,
            )
            if property_findings:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-JSONSCHEMA-DIFF-018",
                        f"removed property '{name}' is no longer accepted by additionalProperties",
                        pointer=f"{pointer}/properties/{escape_json_pointer(name)}",
                        compatibility=direction,
                    )
                )
                findings.extend(property_findings)

    for keyword in ("allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentSchemas"):
        if keyword in after and _freeze(before.get(keyword)) != _freeze(after.get(keyword)):
            findings.append(
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-JSONSCHEMA-DIFF-016",
                    f"composition constraint '{keyword}' changed",
                    pointer=f"{pointer}/{keyword}",
                    compatibility=direction,
                )
            )
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
        if (
            baseline.schema is None
            or candidate.schema is None
            or baseline.context is None
            or candidate.context is None
            or any(item.severity is ContractSeverity.ERROR for item in parse_findings)
        ):
            return tuple(parse_findings)
        if direction is CompatibilityDirection.FORWARD:
            return tuple(
                _one_way_diff(
                    candidate.schema,
                    baseline.schema,
                    before_context=candidate.context,
                    after_context=baseline.context,
                    snapshot=before,
                    pointer="",
                    direction=CompatibilityDirection.FORWARD,
                )
            )
        if direction is CompatibilityDirection.FULL:
            return tuple(
                [
                    *_one_way_diff(
                        baseline.schema,
                        candidate.schema,
                        before_context=baseline.context,
                        after_context=candidate.context,
                        snapshot=after,
                        pointer="",
                        direction=CompatibilityDirection.BACKWARD,
                    ),
                    *_one_way_diff(
                        candidate.schema,
                        baseline.schema,
                        before_context=candidate.context,
                        after_context=baseline.context,
                        snapshot=before,
                        pointer="",
                        direction=CompatibilityDirection.FORWARD,
                    ),
                ]
            )
        return tuple(
            _one_way_diff(
                baseline.schema,
                candidate.schema,
                before_context=baseline.context,
                after_context=candidate.context,
                snapshot=after,
                pointer="",
                direction=CompatibilityDirection.BACKWARD,
            )
        )
