from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re

import yaml

from sdai.path_safety import ensure_within_project
from sdai.text import TextEncodingError, read_utf8_text


class SpecChangeError(RuntimeError):
    """Deterministic error raised for current/change specification contract failures."""


class ChangeStatus(StrEnum):
    DRAFT = "draft"
    PROPOSED = "proposed"


class DeltaOperationKind(StrEnum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    REMOVED = "REMOVED"
    RENAMED = "RENAMED"


_DOMAIN_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_FEATURE_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_REQUIREMENT_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_WINDOWS_RESERVED_PATH_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_CHANGE_KEYS = frozenset(
    {"version", "feature_id", "title", "description", "status", "domains", "baselines"}
)
_DELTA_KEYS = frozenset(
    {"version", "domain", "baseline_spec_sha256", "operations"}
)
_OPERATION_KEYS = frozenset(
    {"op", "requirement_id", "reason", "previous_hash", "new_requirement_id", "definition"}
)


def _fail(code: str, message: str) -> SpecChangeError:
    return SpecChangeError(f"{code}: {message}")


def _validate_identifier(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-SPEC-001", f"{label} must be a non-empty string")
    if value != value.strip():
        raise _fail(
            "SDAI-SPEC-001",
            f"{label} must not contain leading or trailing whitespace",
        )
    if not pattern.fullmatch(value) or ".." in value:
        raise _fail(
            "SDAI-SPEC-001",
            f"{label} '{value}' is not a safe portable identifier",
        )
    return value


def _reject_windows_reserved_path_name(value: str, *, label: str) -> None:
    # Windows treats reserved DOS device names as invalid path components even
    # when an extension is present (for example, CON.txt).
    device_name = value.split(".", 1)[0].upper()
    if device_name in _WINDOWS_RESERVED_PATH_NAMES:
        raise _fail(
            "SDAI-SPEC-001",
            f"{label} '{value}' is a Windows-reserved path name",
        )


def validate_domain_id(value: object) -> str:
    identifier = _validate_identifier(value, label="domain", pattern=_DOMAIN_ID)
    _reject_windows_reserved_path_name(identifier, label="domain")
    return identifier


def validate_change_feature_id(value: object) -> str:
    identifier = _validate_identifier(value, label="feature_id", pattern=_FEATURE_ID)
    _reject_windows_reserved_path_name(identifier, label="feature_id")
    return identifier


def validate_requirement_id(value: object, *, label: str = "requirement_id") -> str:
    return _validate_identifier(value, label=label, pattern=_REQUIREMENT_ID)


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-SPEC-003", f"{label} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, label)


def _parse_hash(value: object, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise _fail(
            "SDAI-SPEC-005",
            f"{label} must use 'sha256:' followed by 64 lowercase hex characters",
        )
    return value


def _unknown_keys(raw: dict[object, object], allowed: frozenset[str]) -> list[str]:
    return sorted(str(key) for key in raw if key not in allowed)


def _normalized_sha256(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _portable_source(project_root: Path, path: Path) -> str:
    root = project_root.resolve()
    safe = ensure_within_project(root, path, label="specification source path")
    return safe.relative_to(root).as_posix()


def _declared_content_root(project_root: Path, relative: str, *, label: str) -> Path:
    root = project_root.resolve()
    if not isinstance(relative, str) or not relative:
        raise _fail("SDAI-SPEC-001", f"{label} must be a non-empty relative path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise _fail("SDAI-SPEC-001", f"{label} must be a safe relative path")
    return ensure_within_project(
        root,
        root.joinpath(*pure.parts),
        label=label,
    )


def current_spec_path(
    project_root: Path,
    domain: str,
    *,
    specification_root: str = "specs/current",
) -> Path:
    root = project_root.resolve()
    domain_id = validate_domain_id(domain)
    content_root = _declared_content_root(
        root,
        specification_root,
        label="current specification root",
    )
    candidate = content_root / domain_id / "specification.md"
    return ensure_within_project(root, candidate, label=f"current specification '{domain_id}'")


def change_dir(
    project_root: Path,
    feature_id: str,
    *,
    changes_root: str = "specs/changes",
) -> Path:
    root = project_root.resolve()
    feature = validate_change_feature_id(feature_id)
    content_root = _declared_content_root(
        root,
        changes_root,
        label="specification changes root",
    )
    candidate = content_root / feature
    return ensure_within_project(root, candidate, label=f"spec change '{feature}'")


def _read_text(project_root: Path, path: Path, label: str) -> str:
    root = project_root.resolve()
    safe = ensure_within_project(root, path, label=label)
    if not safe.is_file():
        raise _fail(
            "SDAI-SPEC-002",
            f"{label} does not exist or is not a file: {_portable_source(root, safe)}",
        )
    try:
        return read_utf8_text(safe)
    except TextEncodingError as exc:
        raise _fail("SDAI-SPEC-002", str(exc)) from exc
    except OSError as exc:
        raise _fail(
            "SDAI-SPEC-002",
            f"unable to read {label}: {exc}",
        ) from exc


def _load_yaml_mapping(
    project_root: Path,
    path: Path,
    label: str,
) -> tuple[dict[object, object], str, Path]:
    root = project_root.resolve()
    safe = ensure_within_project(root, path, label=label)
    text = _read_text(root, safe, label)
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise _fail(
            "SDAI-SPEC-002",
            f"invalid YAML in {_portable_source(root, safe)}: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise _fail("SDAI-SPEC-003", f"{label} must contain a YAML mapping")
    return raw, text, safe


@dataclass(frozen=True)
class CurrentSpecification:
    domain: str
    content: str
    sha256: str
    source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "domain": self.domain,
            "source": self.source,
            "sha256": self.sha256,
            "content": self.content,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class ChangeMetadata:
    feature_id: str
    title: str
    description: str | None
    status: ChangeStatus
    domains: tuple[str, ...]
    baselines: dict[str, str | None]
    source: str
    source_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "feature_id": self.feature_id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "domains": list(self.domains),
            "baselines": {key: self.baselines[key] for key in sorted(self.baselines)},
            "source": self.source,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True)
class DeltaOperation:
    op: DeltaOperationKind
    requirement_id: str
    reason: str
    previous_hash: str | None = None
    new_requirement_id: str | None = None
    definition: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "op": self.op.value,
            "requirement_id": self.requirement_id,
            "reason": self.reason,
        }
        if self.previous_hash is not None:
            payload["previous_hash"] = self.previous_hash
        if self.new_requirement_id is not None:
            payload["new_requirement_id"] = self.new_requirement_id
        if self.definition is not None:
            payload["definition"] = self.definition
        return payload


@dataclass(frozen=True)
class DeltaDocument:
    domain: str
    baseline_spec_sha256: str | None
    operations: tuple[DeltaOperation, ...]
    source: str
    source_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "domain": self.domain,
            "baseline_spec_sha256": self.baseline_spec_sha256,
            "operations": [operation.as_dict() for operation in self.operations],
            "source": self.source,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True)
class SpecChangeBundle:
    metadata: ChangeMetadata
    deltas: tuple[DeltaDocument, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "change": self.metadata.as_dict(),
            "deltas": [delta.as_dict() for delta in self.deltas],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)


def load_current_spec(
    project_root: Path,
    domain: str,
    *,
    specification_root: str = "specs/current",
) -> CurrentSpecification:
    root = project_root.resolve()
    domain_id = validate_domain_id(domain)
    path = current_spec_path(root, domain_id, specification_root=specification_root)
    content = _read_text(root, path, f"current specification '{domain_id}'")
    return CurrentSpecification(
        domain=domain_id,
        content=content,
        sha256=_normalized_sha256(content),
        source=_portable_source(root, path),
    )


def load_change_metadata(
    project_root: Path,
    feature_id: str,
    *,
    changes_root: str = "specs/changes",
) -> ChangeMetadata:
    root = project_root.resolve()
    feature = validate_change_feature_id(feature_id)
    path = change_dir(root, feature, changes_root=changes_root) / "change.yaml"
    raw, text, safe = _load_yaml_mapping(root, path, f"change metadata '{feature}'")
    unknown = _unknown_keys(raw, _CHANGE_KEYS)
    if unknown:
        raise _fail(
            "SDAI-SPEC-003",
            f"change metadata contains unknown field(s): {', '.join(unknown)}",
        )
    if raw.get("version") != 1:
        raise _fail("SDAI-SPEC-003", "change metadata version must be 1")
    declared_feature = validate_change_feature_id(raw.get("feature_id"))
    if declared_feature != feature:
        raise _fail(
            "SDAI-SPEC-003",
            f"change directory '{feature}' does not match feature_id '{declared_feature}'",
        )
    title = _nonempty_string(raw.get("title"), "change.title")
    description = _optional_string(raw.get("description"), "change.description")
    try:
        status = ChangeStatus(str(raw.get("status") or ""))
    except ValueError as exc:
        raise _fail(
            "SDAI-SPEC-003",
            "change.status must be 'draft' or 'proposed'",
        ) from exc
    domains_raw = raw.get("domains")
    if not isinstance(domains_raw, list) or not domains_raw:
        raise _fail("SDAI-SPEC-003", "change.domains must be a non-empty list")
    domains_list = [validate_domain_id(value) for value in domains_raw]
    if len(set(domains_list)) != len(domains_list):
        raise _fail("SDAI-SPEC-003", "change.domains must not contain duplicates")
    domains = tuple(sorted(domains_list))
    baselines_raw = raw.get("baselines")
    if not isinstance(baselines_raw, dict):
        raise _fail(
            "SDAI-SPEC-003",
            "change.baselines must be a mapping keyed by domain",
        )
    baseline_keys = {validate_domain_id(key) for key in baselines_raw}
    if baseline_keys != set(domains):
        missing = sorted(set(domains) - baseline_keys)
        extra = sorted(baseline_keys - set(domains))
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise _fail(
            "SDAI-SPEC-003",
            "change.baselines must define exactly one baseline for every domain"
            + (f" ({'; '.join(details)})" if details else ""),
        )
    baselines = {
        domain: _parse_hash(
            baselines_raw[domain],
            f"change.baselines.{domain}",
            allow_none=True,
        )
        for domain in domains
    }
    return ChangeMetadata(
        feature_id=feature,
        title=title,
        description=description,
        status=status,
        domains=domains,
        baselines=baselines,
        source=_portable_source(root, safe),
        source_sha256=_normalized_sha256(text),
    )


def _operation_kind(value: object, index: int) -> DeltaOperationKind:
    try:
        return DeltaOperationKind(str(value))
    except ValueError as exc:
        supported = ", ".join(item.value for item in DeltaOperationKind)
        raise _fail(
            "SDAI-SPEC-004",
            f"operations[{index}].op must be one of: {supported}",
        ) from exc


def _forbid(
    raw: dict[object, object],
    fields: tuple[str, ...],
    index: int,
    kind: DeltaOperationKind,
) -> None:
    present = [field for field in fields if field in raw]
    if present:
        raise _fail(
            "SDAI-SPEC-004",
            f"operations[{index}] {kind.value} forbids field(s): {', '.join(present)}",
        )


def _parse_operation(raw: object, index: int) -> DeltaOperation:
    if not isinstance(raw, dict):
        raise _fail("SDAI-SPEC-004", f"operations[{index}] must be a mapping")
    unknown = _unknown_keys(raw, _OPERATION_KEYS)
    if unknown:
        raise _fail(
            "SDAI-SPEC-004",
            f"operations[{index}] contains unknown field(s): {', '.join(unknown)}",
        )
    kind = _operation_kind(raw.get("op"), index)
    requirement_id = validate_requirement_id(raw.get("requirement_id"))
    reason = _nonempty_string(raw.get("reason"), f"operations[{index}].reason")

    previous_hash: str | None = None
    new_requirement_id: str | None = None
    definition: str | None = None

    if kind is DeltaOperationKind.ADDED:
        _forbid(raw, ("previous_hash", "new_requirement_id"), index, kind)
        definition = _nonempty_string(
            raw.get("definition"),
            f"operations[{index}].definition",
        )
    elif kind is DeltaOperationKind.MODIFIED:
        _forbid(raw, ("new_requirement_id",), index, kind)
        previous_hash = _parse_hash(
            raw.get("previous_hash"),
            f"operations[{index}].previous_hash",
        )
        definition = _nonempty_string(
            raw.get("definition"),
            f"operations[{index}].definition",
        )
    elif kind is DeltaOperationKind.REMOVED:
        _forbid(raw, ("new_requirement_id", "definition"), index, kind)
        previous_hash = _parse_hash(
            raw.get("previous_hash"),
            f"operations[{index}].previous_hash",
        )
    else:
        _forbid(raw, ("definition",), index, kind)
        previous_hash = _parse_hash(
            raw.get("previous_hash"),
            f"operations[{index}].previous_hash",
        )
        new_requirement_id = validate_requirement_id(
            raw.get("new_requirement_id"),
            label=f"operations[{index}].new_requirement_id",
        )
        if new_requirement_id == requirement_id:
            raise _fail(
                "SDAI-SPEC-004",
                f"operations[{index}] RENAMED new_requirement_id must differ from requirement_id",
            )

    return DeltaOperation(
        op=kind,
        requirement_id=requirement_id,
        reason=reason,
        previous_hash=previous_hash,
        new_requirement_id=new_requirement_id,
        definition=definition,
    )


def load_delta_document(project_root: Path, path: Path) -> DeltaDocument:
    root = project_root.resolve()
    raw, text, safe = _load_yaml_mapping(root, path, "delta document")
    unknown = _unknown_keys(raw, _DELTA_KEYS)
    if unknown:
        raise _fail(
            "SDAI-SPEC-003",
            f"delta document contains unknown field(s): {', '.join(unknown)}",
        )
    if raw.get("version") != 1:
        raise _fail("SDAI-SPEC-003", "delta document version must be 1")
    domain = validate_domain_id(raw.get("domain"))
    if "baseline_spec_sha256" not in raw:
        raise _fail(
            "SDAI-SPEC-003",
            "delta document must declare baseline_spec_sha256 (use null for a new domain)",
        )
    baseline = _parse_hash(
        raw.get("baseline_spec_sha256"),
        "delta.baseline_spec_sha256",
        allow_none=True,
    )
    operations_raw = raw.get("operations")
    if not isinstance(operations_raw, list) or not operations_raw:
        raise _fail("SDAI-SPEC-003", "delta.operations must be a non-empty list")
    operations = tuple(
        _parse_operation(item, index)
        for index, item in enumerate(operations_raw, start=1)
    )
    addressed_ids: list[str] = []
    for operation in operations:
        addressed_ids.append(operation.requirement_id)
        if operation.new_requirement_id is not None:
            addressed_ids.append(operation.new_requirement_id)
    duplicates = sorted(
        {identifier for identifier in addressed_ids if addressed_ids.count(identifier) > 1}
    )
    if duplicates:
        raise _fail(
            "SDAI-SPEC-006",
            "delta document contains multiple operations for the same requirement_id "
            "or rename destination: " + ", ".join(duplicates),
        )
    return DeltaDocument(
        domain=domain,
        baseline_spec_sha256=baseline,
        operations=operations,
        source=_portable_source(root, safe),
        source_sha256=_normalized_sha256(text),
    )


def load_spec_change(
    project_root: Path,
    feature_id: str,
    *,
    changes_root: str = "specs/changes",
) -> SpecChangeBundle:
    root = project_root.resolve()
    metadata = load_change_metadata(root, feature_id, changes_root=changes_root)
    delta_root = ensure_within_project(
        root,
        change_dir(root, metadata.feature_id, changes_root=changes_root) / "deltas",
        label=f"delta directory '{metadata.feature_id}'",
    )
    if not delta_root.is_dir():
        raise _fail(
            "SDAI-SPEC-002",
            f"delta directory does not exist: {_portable_source(root, delta_root)}",
        )
    paths = sorted(
        [*delta_root.glob("*.yaml"), *delta_root.glob("*.yml")],
        key=lambda item: item.name.casefold(),
    )
    if not paths:
        raise _fail(
            "SDAI-SPEC-002",
            f"no delta documents found for change '{metadata.feature_id}'",
        )
    contained_paths = tuple(
        ensure_within_project(
            delta_root,
            path,
            label=f"delta document '{metadata.feature_id}'",
        )
        for path in paths
    )
    deltas = tuple(load_delta_document(root, path) for path in contained_paths)
    by_domain: dict[str, DeltaDocument] = {}
    for delta in deltas:
        if delta.domain not in metadata.domains:
            raise _fail(
                "SDAI-SPEC-007",
                f"delta domain '{delta.domain}' is not declared by change '{metadata.feature_id}'",
            )
        if delta.domain in by_domain:
            raise _fail(
                "SDAI-SPEC-007",
                f"change '{metadata.feature_id}' contains more than one delta document for domain '{delta.domain}'",
            )
        if delta.baseline_spec_sha256 != metadata.baselines[delta.domain]:
            raise _fail(
                "SDAI-SPEC-007",
                f"delta baseline for domain '{delta.domain}' does not match change.yaml baseline",
            )
        by_domain[delta.domain] = delta
    missing = sorted(set(metadata.domains) - set(by_domain))
    if missing:
        raise _fail(
            "SDAI-SPEC-007",
            "change is missing delta documents for domain(s): " + ", ".join(missing),
        )
    ordered = tuple(by_domain[domain] for domain in metadata.domains)
    return SpecChangeBundle(metadata=metadata, deltas=ordered)
