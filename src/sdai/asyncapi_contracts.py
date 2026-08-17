from __future__ import annotations

from dataclasses import dataclass
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


_ASYNCAPI_VERSION = re.compile(r"^(?:2|3)\.\d+\.\d+(?:[-+].*)?$")


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
    compatibility: CompatibilityDirection = CompatibilityDirection.NONE,
) -> ContractFinding:
    return ContractFinding(
        code=code,
        severity=ContractSeverity.ERROR,
        message=message,
        compatibility=compatibility,
        provenance=_provenance(snapshot, pointer),
    )


def _dereference(value: object, document: Mapping[str, object], *, seen: frozenset[str] = frozenset()) -> object:
    current = value
    for _ in range(32):
        if not isinstance(current, Mapping) or not isinstance(current.get("$ref"), str):
            return current
        reference = current["$ref"]
        if not reference.startswith("#/") or reference in seen:
            return current
        seen = seen | {reference}
        try:
            current = resolve_local_json_pointer(document, reference)
        except (KeyError, ValueError):
            return current
    return current


def _scan_refs(
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
            ref_pointer = f"{pointer}/$ref"
            if not isinstance(reference, str):
                findings.append(_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-006", "$ref must be a string", pointer=ref_pointer))
            elif not reference.startswith("#/"):
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-ASYNCAPI-007",
                        f"external or non-local reference is not allowed: {reference}",
                        pointer=ref_pointer,
                    )
                )
            else:
                try:
                    resolve_local_json_pointer(document, reference)
                except (KeyError, ValueError):
                    findings.append(
                        _finding(
                            snapshot,
                            "SDAI-CONTRACT-ASYNCAPI-008",
                            f"unresolved local reference: {reference}",
                            pointer=ref_pointer,
                        )
                    )
        for key in sorted(value):
            findings.extend(
                _scan_refs(
                    snapshot,
                    document,
                    value[key],
                    pointer=f"{pointer}/{escape_json_pointer(key)}",
                )
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_scan_refs(snapshot, document, item, pointer=f"{pointer}/{index}"))
    return findings


def _parse(snapshot: ContractSnapshot) -> _Parsed:
    try:
        raw = yaml.load(snapshot.text, Loader=UniqueKeySafeLoader)
    except yaml.YAMLError:
        return _Parsed(None, (_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-001", "AsyncAPI document is not valid YAML/JSON"),))
    try:
        normalized = normalize_structured_json(raw)
    except ValueError as exc:
        return _Parsed(None, (_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-002", str(exc)),))
    if not isinstance(normalized, Mapping):
        return _Parsed(None, (_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-002", "AsyncAPI document root must be a mapping"),))

    document = normalized
    findings: list[ContractFinding] = []
    version = document.get("asyncapi")
    if not isinstance(version, str) or not _ASYNCAPI_VERSION.fullmatch(version):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-ASYNCAPI-003",
                "AsyncAPI document must declare a supported 2.x or 3.x version",
                pointer="/asyncapi",
            )
        )
    info = document.get("info")
    if not isinstance(info, Mapping) or not isinstance(info.get("title"), str) or not isinstance(info.get("version"), str):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-ASYNCAPI-004",
                "AsyncAPI info must contain string title and version fields",
                pointer="/info",
            )
        )
    channels = document.get("channels")
    if not isinstance(channels, Mapping):
        findings.append(_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-005", "AsyncAPI channels must be a mapping", pointer="/channels"))
        channels = {}
    for name in sorted(channels):
        channel = _dereference(channels[name], document)
        if not isinstance(channel, Mapping):
            findings.append(
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-ASYNCAPI-009",
                    f"channel '{name}' must be a mapping",
                    pointer=f"/channels/{escape_json_pointer(name)}",
                )
            )
            continue
        for action in ("publish", "subscribe"):
            operation = channel.get(action)
            if operation is not None and not isinstance(_dereference(operation, document), Mapping):
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-ASYNCAPI-010",
                        f"channel operation '{action}' must be a mapping",
                        pointer=f"/channels/{escape_json_pointer(name)}/{action}",
                    )
                )
    operations = document.get("operations")
    if operations is not None and not isinstance(operations, Mapping):
        findings.append(_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-011", "AsyncAPI operations must be a mapping", pointer="/operations"))
    findings.extend(_scan_refs(snapshot, document, document))
    return _Parsed(
        document,
        tuple(sorted(findings, key=lambda item: (item.code, item.provenance.pointer or "", item.message))),
    )


def _operations(document: Mapping[str, object]) -> dict[tuple[str, str], Mapping[str, object]]:
    result: dict[tuple[str, str], Mapping[str, object]] = {}
    channels = document.get("channels")
    if isinstance(channels, Mapping):
        for channel_name in sorted(channels):
            channel = _dereference(channels[channel_name], document)
            if not isinstance(channel, Mapping):
                continue
            for action in ("publish", "subscribe"):
                operation = _dereference(channel.get(action), document)
                if isinstance(operation, Mapping):
                    result[(str(channel_name), action)] = operation

    top_operations = document.get("operations")
    if isinstance(top_operations, Mapping):
        for operation_id in sorted(top_operations):
            operation = _dereference(top_operations[operation_id], document)
            if not isinstance(operation, Mapping):
                continue
            action = operation.get("action")
            channel_ref = operation.get("channel")
            channel_name = str(operation_id)
            if isinstance(channel_ref, Mapping) and isinstance(channel_ref.get("$ref"), str):
                reference = channel_ref["$ref"]
                if reference.startswith("#/channels/"):
                    channel_name = reference.removeprefix("#/channels/").replace("~1", "/").replace("~0", "~")
            if isinstance(action, str):
                result[(channel_name, action)] = operation
    return result


def _message(operation: Mapping[str, object], document: Mapping[str, object]) -> Mapping[str, object] | None:
    raw = operation.get("message")
    if raw is None:
        messages = operation.get("messages")
        if isinstance(messages, list) and messages:
            raw = messages[0]
        elif isinstance(messages, Mapping) and messages:
            raw = messages[sorted(messages)[0]]
    resolved = _dereference(raw, document)
    if isinstance(resolved, Mapping) and isinstance(resolved.get("oneOf"), list) and resolved["oneOf"]:
        resolved = _dereference(resolved["oneOf"][0], document)
    return resolved if isinstance(resolved, Mapping) else None


def _schema(value: object, document: Mapping[str, object]) -> Mapping[str, object] | None:
    resolved = _dereference(value, document)
    return resolved if isinstance(resolved, Mapping) else None


def _freeze(value: object) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _schema_diff(
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
    *,
    before_doc: Mapping[str, object],
    after_doc: Mapping[str, object],
    snapshot: ContractSnapshot,
    pointer: str,
    direction: CompatibilityDirection,
) -> list[ContractFinding]:
    if before is None or after is None:
        if before != after:
            return [_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-DIFF-020", "message payload schema presence changed", pointer=pointer, compatibility=direction)]
        return []
    before = _schema(before, before_doc) or before
    after = _schema(after, after_doc) or after
    findings: list[ContractFinding] = []
    old_type = before.get("type", "object" if "properties" in before else None)
    new_type = after.get("type", "object" if "properties" in after else None)
    if old_type != new_type:
        return [_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-DIFF-021", f"payload type changed from {old_type!r} to {new_type!r}", pointer=pointer, compatibility=direction)]
    old_enum, new_enum = before.get("enum"), after.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list):
        if not {_freeze(item) for item in old_enum} <= {_freeze(item) for item in new_enum}:
            findings.append(_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-DIFF-022", "payload enum removed previously accepted value(s)", pointer=f"{pointer}/enum", compatibility=direction))
    old_required = set(before.get("required", [])) if isinstance(before.get("required"), list) else set()
    new_required = set(after.get("required", [])) if isinstance(after.get("required"), list) else set()
    for name in sorted(new_required - old_required):
        findings.append(_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-DIFF-023", f"payload property '{name}' became required", pointer=f"{pointer}/required", compatibility=direction))
    old_props = before.get("properties") if isinstance(before.get("properties"), Mapping) else {}
    new_props = after.get("properties") if isinstance(after.get("properties"), Mapping) else {}
    for name in sorted(set(old_props) & set(new_props)):
        findings.extend(
            _schema_diff(
                _schema(old_props[name], before_doc),
                _schema(new_props[name], after_doc),
                before_doc=before_doc,
                after_doc=after_doc,
                snapshot=snapshot,
                pointer=f"{pointer}/properties/{escape_json_pointer(name)}",
                direction=direction,
            )
        )
    return findings


def _binding_fingerprint(value: object) -> str:
    return _freeze(value) if value is not None else "null"


def _diff_one_way(
    before: Mapping[str, object],
    after: Mapping[str, object],
    snapshot: ContractSnapshot,
    direction: CompatibilityDirection,
) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    old_ops, new_ops = _operations(before), _operations(after)
    old_channels = {channel for channel, _ in old_ops}
    new_channels = {channel for channel, _ in new_ops}
    for channel in sorted(old_channels - new_channels):
        findings.append(_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-DIFF-001", f"channel removed: {channel}", pointer=f"/channels/{escape_json_pointer(channel)}", compatibility=direction))
    for channel, action in sorted(set(old_ops) - set(new_ops)):
        findings.append(_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-DIFF-002", f"operation removed: {action} {channel}", pointer=f"/channels/{escape_json_pointer(channel)}/{action}", compatibility=direction))
    for key in sorted(set(old_ops) & set(new_ops)):
        channel, action = key
        old_operation, new_operation = old_ops[key], new_ops[key]
        old_message = _message(old_operation, before)
        new_message = _message(new_operation, after)
        pointer = f"/channels/{escape_json_pointer(channel)}/{action}/message"
        if old_message is not None and new_message is None:
            findings.append(_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-DIFF-003", "operation message removed", pointer=pointer, compatibility=direction))
            continue
        if old_message is None or new_message is None:
            continue
        findings.extend(
            _schema_diff(
                _schema(old_message.get("payload"), before),
                _schema(new_message.get("payload"), after),
                before_doc=before,
                after_doc=after,
                snapshot=snapshot,
                pointer=f"{pointer}/payload",
                direction=direction,
            )
        )
        findings.extend(
            _schema_diff(
                _schema(old_message.get("headers"), before),
                _schema(new_message.get("headers"), after),
                before_doc=before,
                after_doc=after,
                snapshot=snapshot,
                pointer=f"{pointer}/headers",
                direction=direction,
            )
        )
        if _binding_fingerprint(old_message.get("bindings")) != _binding_fingerprint(new_message.get("bindings")):
            findings.append(_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-DIFF-004", "message bindings changed", pointer=f"{pointer}/bindings", compatibility=direction))
        if _binding_fingerprint(old_operation.get("bindings")) != _binding_fingerprint(new_operation.get("bindings")):
            findings.append(_finding(snapshot, "SDAI-CONTRACT-ASYNCAPI-DIFF-005", "operation bindings changed", pointer=f"/channels/{escape_json_pointer(channel)}/{action}/bindings", compatibility=direction))
    return findings


class AsyncAPIContractAdapter:
    kind = "asyncapi"

    def check(self, snapshot: ContractSnapshot) -> Sequence[ContractFinding]:
        return _parse(snapshot).findings

    def diff(
        self,
        before: ContractSnapshot,
        after: ContractSnapshot,
        direction: CompatibilityDirection,
    ) -> Sequence[ContractFinding]:
        baseline, candidate = _parse(before), _parse(after)
        invalid = [*baseline.findings, *candidate.findings]
        if baseline.document is None or candidate.document is None or invalid:
            return tuple(invalid)
        if direction is CompatibilityDirection.FORWARD:
            return tuple(_diff_one_way(candidate.document, baseline.document, before, CompatibilityDirection.FORWARD))
        if direction is CompatibilityDirection.FULL:
            return tuple(
                [
                    *_diff_one_way(baseline.document, candidate.document, after, CompatibilityDirection.BACKWARD),
                    *_diff_one_way(candidate.document, baseline.document, before, CompatibilityDirection.FORWARD),
                ]
            )
        return tuple(_diff_one_way(baseline.document, candidate.document, after, CompatibilityDirection.BACKWARD))
