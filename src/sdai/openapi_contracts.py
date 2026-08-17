from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from sdai.contracts import (
    CompatibilityDirection,
    ContractFinding,
    ContractProvenance,
    ContractSeverity,
    ContractSnapshot,
)


_HTTP_METHODS = ("delete", "get", "head", "options", "patch", "post", "put", "trace")
_OPENAPI_VERSION = re.compile(r"^3\.(?:0|1)\.\d+(?:[-+].*)?$")
_PATH_PARAMETER = re.compile(r"\{([^{}]+)\}")
_MAX_NODES = 200_000
_MAX_DEPTH = 64


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found an unhashable mapping key", key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found a duplicate mapping key", key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


@dataclass(frozen=True, slots=True)
class _Parsed:
    document: Mapping[str, object] | None
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


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _normalize_json(
    value: object,
    *,
    pointer: str,
    depth: int = 0,
    ancestors: frozenset[int] = frozenset(),
    counter: list[int] | None = None,
) -> object:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > _MAX_NODES:
        raise ValueError("document exceeds the maximum value count")
    if depth > _MAX_DEPTH:
        raise ValueError("document exceeds the maximum nesting depth")
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
            raise ValueError(f"{pointer or '/'} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in ancestors:
            raise ValueError(f"{pointer or '/'} contains a recursive YAML alias")
        next_ancestors = ancestors | {identity}
        normalized: dict[str, object] = {}
        for key in sorted(value, key=lambda item: str(item)):
            if not isinstance(key, str):
                raise ValueError(f"{pointer or '/'} contains a non-string mapping key")
            child = f"{pointer}/{_escape_pointer(key)}" if pointer else f"/{_escape_pointer(key)}"
            normalized[key] = _normalize_json(
                value[key],
                pointer=child,
                depth=depth + 1,
                ancestors=next_ancestors,
                counter=counter,
            )
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in ancestors:
            raise ValueError(f"{pointer or '/'} contains a recursive YAML alias")
        next_ancestors = ancestors | {identity}
        return [
            _normalize_json(
                item,
                pointer=f"{pointer}/{index}" if pointer else f"/{index}",
                depth=depth + 1,
                ancestors=next_ancestors,
                counter=counter,
            )
            for index, item in enumerate(value)
        ]
    raise ValueError(f"{pointer or '/'} contains an unsupported YAML value")


def _resolve_pointer(document: Mapping[str, object], reference: str) -> object:
    if not reference.startswith("#/"):
        raise ValueError("external references are not allowed")
    current: object = document
    for raw in reference[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise KeyError(reference)
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                raise KeyError(reference)
            current = current[int(token)]
        else:
            raise KeyError(reference)
    return current


def _scan_references(
    snapshot: ContractSnapshot,
    document: Mapping[str, object],
    value: object,
    *,
    pointer: str = "",
) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    if isinstance(value, Mapping):
        reference = value.get("$ref")
        if reference is not None:
            if not isinstance(reference, str):
                findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-006", "$ref must be a string", pointer=f"{pointer}/$ref"))
            elif not reference.startswith("#/"):
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-OPENAPI-007",
                        f"external or non-local reference is not allowed: {reference}",
                        pointer=f"{pointer}/$ref",
                    )
                )
            else:
                try:
                    _resolve_pointer(document, reference)
                except (KeyError, ValueError):
                    findings.append(
                        _finding(
                            snapshot,
                            "SDAI-CONTRACT-OPENAPI-008",
                            f"unresolved local reference: {reference}",
                            pointer=f"{pointer}/$ref",
                        )
                    )
        for key in sorted(value):
            child = f"{pointer}/{_escape_pointer(key)}"
            findings.extend(_scan_references(snapshot, document, value[key], pointer=child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_scan_references(snapshot, document, item, pointer=f"{pointer}/{index}"))
    return findings


def _parse(snapshot: ContractSnapshot) -> _Parsed:
    findings: list[ContractFinding] = []
    try:
        raw = yaml.load(snapshot.text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError:
        return _Parsed(None, (_finding(snapshot, "SDAI-CONTRACT-OPENAPI-001", "OpenAPI document is not valid YAML/JSON"),))
    try:
        normalized = _normalize_json(raw, pointer="")
    except ValueError as exc:
        return _Parsed(None, (_finding(snapshot, "SDAI-CONTRACT-OPENAPI-002", str(exc)),))
    if not isinstance(normalized, Mapping):
        return _Parsed(None, (_finding(snapshot, "SDAI-CONTRACT-OPENAPI-002", "OpenAPI document root must be a mapping"),))
    document = normalized
    version = document.get("openapi")
    if not isinstance(version, str) or not _OPENAPI_VERSION.fullmatch(version):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-OPENAPI-003",
                "OpenAPI document must declare a supported openapi 3.0.x or 3.1.x version",
                pointer="/openapi",
            )
        )
    info = document.get("info")
    if not isinstance(info, Mapping) or not isinstance(info.get("title"), str) or not isinstance(info.get("version"), str):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-OPENAPI-004",
                "OpenAPI info must contain string title and version fields",
                pointer="/info",
            )
        )
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        findings.append(
            _finding(snapshot, "SDAI-CONTRACT-OPENAPI-005", "OpenAPI paths must be a mapping", pointer="/paths")
        )
        paths = {}
    operation_ids: dict[str, str] = {}
    for path in sorted(paths):
        path_item = paths[path]
        path_pointer = f"/paths/{_escape_pointer(path)}"
        if not isinstance(path, str) or not path.startswith("/"):
            findings.append(
                _finding(snapshot, "SDAI-CONTRACT-OPENAPI-009", f"path key must start with '/': {path}", pointer=path_pointer)
            )
            continue
        if not isinstance(path_item, Mapping):
            findings.append(
                _finding(snapshot, "SDAI-CONTRACT-OPENAPI-010", f"path item must be a mapping: {path}", pointer=path_pointer)
            )
            continue
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if operation is None:
                continue
            operation_pointer = f"{path_pointer}/{method}"
            if not isinstance(operation, Mapping):
                findings.append(
                    _finding(snapshot, "SDAI-CONTRACT-OPENAPI-011", f"operation {method.upper()} {path} must be a mapping", pointer=operation_pointer)
                )
                continue
            responses = operation.get("responses")
            if not isinstance(responses, Mapping) or not responses:
                findings.append(
                    _finding(snapshot, "SDAI-CONTRACT-OPENAPI-012", f"operation {method.upper()} {path} must declare responses", pointer=f"{operation_pointer}/responses")
                )
            operation_id = operation.get("operationId")
            if operation_id is not None:
                if not isinstance(operation_id, str) or not operation_id.strip():
                    findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-013", "operationId must be a non-empty string", pointer=f"{operation_pointer}/operationId"))
                elif operation_id in operation_ids:
                    findings.append(
                        _finding(snapshot, "SDAI-CONTRACT-OPENAPI-014", f"duplicate operationId '{operation_id}'", pointer=f"{operation_pointer}/operationId")
                    )
                else:
                    operation_ids[operation_id] = operation_pointer
            parameters = _merged_parameters(path_item, operation, document)
            placeholders = set(_PATH_PARAMETER.findall(path))
            path_parameters = {
                item.get("name")
                for item in parameters
                if isinstance(item, Mapping) and item.get("in") == "path"
            }
            for name in sorted(placeholders - path_parameters):
                findings.append(
                    _finding(snapshot, "SDAI-CONTRACT-OPENAPI-015", f"path parameter '{{{name}}}' is not declared", pointer=operation_pointer)
                )
            for parameter in parameters:
                if isinstance(parameter, Mapping) and parameter.get("in") == "path" and parameter.get("required") is not True:
                    findings.append(
                        _finding(snapshot, "SDAI-CONTRACT-OPENAPI-016", f"path parameter '{parameter.get('name')}' must be required", pointer=operation_pointer)
                    )
    findings.extend(_scan_references(snapshot, document, document))
    return _Parsed(document, tuple(sorted(findings, key=lambda item: (item.code, item.provenance.pointer or "", item.message))))


def _dereference(value: object, document: Mapping[str, object], *, depth: int = 0, seen: frozenset[str] = frozenset()) -> object:
    if depth > 32:
        return value
    if isinstance(value, Mapping) and isinstance(value.get("$ref"), str):
        reference = value["$ref"]
        if not reference.startswith("#/") or reference in seen:
            return value
        try:
            target = _resolve_pointer(document, reference)
        except (KeyError, ValueError):
            return value
        return _dereference(target, document, depth=depth + 1, seen=seen | {reference})
    return value


def _merged_parameters(path_item: Mapping[str, object], operation: Mapping[str, object], document: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    merged: dict[tuple[str, str], Mapping[str, object]] = {}
    for raw_collection in (path_item.get("parameters", []), operation.get("parameters", [])):
        if not isinstance(raw_collection, list):
            continue
        for raw in raw_collection:
            item = _dereference(raw, document)
            if not isinstance(item, Mapping):
                continue
            name, location = item.get("name"), item.get("in")
            if isinstance(name, str) and isinstance(location, str):
                merged[(location, name)] = item
    return tuple(merged[key] for key in sorted(merged))


def _operations(document: Mapping[str, object]) -> dict[tuple[str, str], Mapping[str, object]]:
    result = {}
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        return result
    for path in sorted(paths):
        item = paths[path]
        if not isinstance(path, str) or not isinstance(item, Mapping):
            continue
        for method in _HTTP_METHODS:
            operation = item.get(method)
            if isinstance(operation, Mapping):
                result[(path, method)] = operation
    return result


def _schema(value: object, document: Mapping[str, object]) -> Mapping[str, object] | None:
    resolved = _dereference(value, document)
    return resolved if isinstance(resolved, Mapping) else None


def _schema_type(schema: Mapping[str, object]) -> object:
    value = schema.get("type")
    if value is None and "properties" in schema:
        return "object"
    return value


def _schema_findings(
    baseline: Mapping[str, object] | None,
    candidate: Mapping[str, object] | None,
    *,
    baseline_doc: Mapping[str, object],
    candidate_doc: Mapping[str, object],
    snapshot: ContractSnapshot,
    pointer: str,
    context: str,
    direction: CompatibilityDirection,
) -> list[ContractFinding]:
    if baseline is None or candidate is None:
        if baseline != candidate:
            return [_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-020", f"{context} schema presence changed", pointer=pointer, compatibility=direction)]
        return []
    baseline = _schema(baseline, baseline_doc) or baseline
    candidate = _schema(candidate, candidate_doc) or candidate
    findings: list[ContractFinding] = []
    if _schema_type(baseline) != _schema_type(candidate):
        findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-021", f"{context} schema type changed from {_schema_type(baseline)!r} to {_schema_type(candidate)!r}", pointer=pointer, compatibility=direction))
        return findings
    old_enum = baseline.get("enum")
    new_enum = candidate.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list):
        old = {_freeze(item) for item in old_enum}
        new = {_freeze(item) for item in new_enum}
        if context == "request" and not old <= new:
            findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-022", "request enum removed previously accepted value(s)", pointer=f"{pointer}/enum", compatibility=direction))
        if context == "response" and not new <= old:
            findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-023", "response enum added value(s) not present in baseline", pointer=f"{pointer}/enum", compatibility=direction))
    old_required = set(baseline.get("required", [])) if isinstance(baseline.get("required"), list) else set()
    new_required = set(candidate.get("required", [])) if isinstance(candidate.get("required"), list) else set()
    if context == "request":
        for name in sorted(new_required - old_required):
            findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-024", f"request property '{name}' became required", pointer=f"{pointer}/required", compatibility=direction))
    else:
        for name in sorted(old_required - new_required):
            findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-025", f"required response property '{name}' is no longer guaranteed", pointer=f"{pointer}/required", compatibility=direction))
    old_props = baseline.get("properties") if isinstance(baseline.get("properties"), Mapping) else {}
    new_props = candidate.get("properties") if isinstance(candidate.get("properties"), Mapping) else {}
    for name in sorted(set(old_props) & set(new_props)):
        findings.extend(
            _schema_findings(
                _schema(old_props[name], baseline_doc),
                _schema(new_props[name], candidate_doc),
                baseline_doc=baseline_doc,
                candidate_doc=candidate_doc,
                snapshot=snapshot,
                pointer=f"{pointer}/properties/{_escape_pointer(name)}",
                context=context,
                direction=direction,
            )
        )
    if context == "response":
        for name in sorted(set(old_props) - set(new_props)):
            findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-026", f"response property '{name}' was removed", pointer=f"{pointer}/properties/{_escape_pointer(name)}", compatibility=direction))
    return findings


def _freeze(value: object) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_schema(container: object, document: Mapping[str, object]) -> Mapping[str, object] | None:
    resolved = _dereference(container, document)
    if not isinstance(resolved, Mapping):
        return None
    content = resolved.get("content")
    if not isinstance(content, Mapping):
        schema = resolved.get("schema")
        return _schema(schema, document)
    for media_type in sorted(content):
        media = content[media_type]
        if isinstance(media, Mapping) and "schema" in media:
            return _schema(media.get("schema"), document)
    return None


def _security_fingerprint(value: object) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return ()
    result = []
    for requirement in value:
        if not isinstance(requirement, Mapping):
            continue
        for name in sorted(requirement):
            scopes = requirement[name]
            normalized = tuple(sorted(str(scope) for scope in scopes)) if isinstance(scopes, list) else ()
            result.append((name, normalized))
    return tuple(result)


def _diff_documents(
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
    snapshot: ContractSnapshot,
    direction: CompatibilityDirection,
) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    old_ops, new_ops = _operations(baseline), _operations(candidate)
    for key in sorted(set(old_ops) - set(new_ops)):
        path, method = key
        findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-001", f"operation removed: {method.upper()} {path}", pointer=f"/paths/{_escape_pointer(path)}/{method}", compatibility=direction))
    old_paths = {path for path, _ in old_ops}
    new_paths = {path for path, _ in new_ops}
    for path in sorted(old_paths - new_paths):
        findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-002", f"path removed: {path}", pointer=f"/paths/{_escape_pointer(path)}", compatibility=direction))
    paths_old = baseline.get("paths") if isinstance(baseline.get("paths"), Mapping) else {}
    paths_new = candidate.get("paths") if isinstance(candidate.get("paths"), Mapping) else {}
    for path, method in sorted(set(old_ops) & set(new_ops)):
        old_op, new_op = old_ops[(path, method)], new_ops[(path, method)]
        old_path_item = paths_old.get(path, {})
        new_path_item = paths_new.get(path, {})
        old_params = {(item.get("in"), item.get("name")): item for item in _merged_parameters(old_path_item, old_op, baseline)}
        new_params = {(item.get("in"), item.get("name")): item for item in _merged_parameters(new_path_item, new_op, candidate)}
        op_pointer = f"/paths/{_escape_pointer(path)}/{method}"
        for param_key in sorted(set(new_params) - set(old_params), key=str):
            item = new_params[param_key]
            if item.get("required") is True:
                findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-010", f"required request parameter added: {param_key[0]} {param_key[1]}", pointer=f"{op_pointer}/parameters", compatibility=direction))
        for param_key in sorted(set(old_params) & set(new_params), key=str):
            old_param, new_param = old_params[param_key], new_params[param_key]
            if old_param.get("required") is not True and new_param.get("required") is True:
                findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-011", f"request parameter became required: {param_key[0]} {param_key[1]}", pointer=f"{op_pointer}/parameters", compatibility=direction))
            findings.extend(_schema_findings(_schema(old_param.get("schema"), baseline), _schema(new_param.get("schema"), candidate), baseline_doc=baseline, candidate_doc=candidate, snapshot=snapshot, pointer=f"{op_pointer}/parameters/{_escape_pointer(str(param_key[1]))}/schema", context="request", direction=direction))
        old_body = _dereference(old_op.get("requestBody"), baseline)
        new_body = _dereference(new_op.get("requestBody"), candidate)
        old_required = isinstance(old_body, Mapping) and old_body.get("required") is True
        new_required = isinstance(new_body, Mapping) and new_body.get("required") is True
        if not old_required and new_required:
            findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-012", "request body became required", pointer=f"{op_pointer}/requestBody", compatibility=direction))
        findings.extend(_schema_findings(_content_schema(old_body, baseline), _content_schema(new_body, candidate), baseline_doc=baseline, candidate_doc=candidate, snapshot=snapshot, pointer=f"{op_pointer}/requestBody", context="request", direction=direction))
        old_responses = old_op.get("responses") if isinstance(old_op.get("responses"), Mapping) else {}
        new_responses = new_op.get("responses") if isinstance(new_op.get("responses"), Mapping) else {}
        for status in sorted(set(old_responses) - set(new_responses)):
            findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-013", f"response removed: {status}", pointer=f"{op_pointer}/responses/{_escape_pointer(str(status))}", compatibility=direction))
        for status in sorted(set(old_responses) & set(new_responses)):
            findings.extend(_schema_findings(_content_schema(old_responses[status], baseline), _content_schema(new_responses[status], candidate), baseline_doc=baseline, candidate_doc=candidate, snapshot=snapshot, pointer=f"{op_pointer}/responses/{_escape_pointer(str(status))}", context="response", direction=direction))
        old_security = _security_fingerprint(old_op.get("security", baseline.get("security")))
        new_security = _security_fingerprint(new_op.get("security", candidate.get("security")))
        if old_security != new_security and new_security:
            findings.append(_finding(snapshot, "SDAI-CONTRACT-OPENAPI-DIFF-014", "operation security requirements changed or became more restrictive", pointer=f"{op_pointer}/security", compatibility=direction))
    return findings


class OpenAPIContractAdapter:
    kind = "openapi"

    def check(self, snapshot: ContractSnapshot) -> Sequence[ContractFinding]:
        return _parse(snapshot).findings

    def diff(
        self,
        before: ContractSnapshot,
        after: ContractSnapshot,
        direction: CompatibilityDirection,
    ) -> Sequence[ContractFinding]:
        baseline = _parse(before)
        candidate = _parse(after)
        invalid = [*baseline.findings, *candidate.findings]
        if baseline.document is None or candidate.document is None or any(
            item.severity is ContractSeverity.ERROR for item in invalid
        ):
            return tuple(invalid)
        findings = _diff_documents(baseline.document, candidate.document, after, direction)
        if direction is CompatibilityDirection.FULL:
            reverse = _diff_documents(candidate.document, baseline.document, before, CompatibilityDirection.FORWARD)
            findings.extend(reverse)
        return tuple(findings)
