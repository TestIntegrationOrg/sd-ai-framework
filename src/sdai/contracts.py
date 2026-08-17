from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Mapping, Protocol, Sequence
import unicodedata

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver


CONTRACT_SOURCE_API_VERSION = "sdai.contract-sources/v1"
CONTRACT_SNAPSHOT_API_VERSION = "sdai.contract-snapshot/v1"
CONTRACT_RESULT_API_VERSION = "sdai.contract-result/v1"
CONTRACT_DIFF_API_VERSION = "sdai.contract-diff/v1"
CONTRACT_MANIFEST_PATH = ".sdai/contracts.yaml"
CONTRACT_MANIFEST_MAX_BYTES = 1024 * 1024
CONTRACT_SOURCE_MAX_BYTES = 16 * 1024 * 1024
SUPPORTED_CONTRACT_KINDS = frozenset({"openapi", "asyncapi", "json-schema", "protobuf"})

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')


class ContractError(RuntimeError):
    """Stable machine-readable contract engineering failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}

    def to_json(self) -> str:
        return _canonical_json({"error": self.to_dict()}) + "\n"


class ContractSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CompatibilityDirection(StrEnum):
    NONE = "none"
    BACKWARD = "backward"
    FORWARD = "forward"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class ContractSource:
    source_id: str
    kind: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.source_id, "kind": self.kind, "path": self.path}


@dataclass(frozen=True, slots=True)
class ContractSnapshot:
    source: ContractSource
    sha256: str
    size_bytes: int
    text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "apiVersion": CONTRACT_SNAPSHOT_API_VERSION,
            "kind": "ContractSnapshot",
            "source": self.source.to_dict(),
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class ContractProvenance:
    source_id: str
    source_path: str
    source_sha256: str
    pointer: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "sourceId": self.source_id,
            "sourcePath": self.source_path,
            "sourceSha256": self.source_sha256,
        }
        if self.pointer is not None:
            payload["pointer"] = self.pointer
        return payload


@dataclass(frozen=True, slots=True)
class ContractFinding:
    code: str
    severity: ContractSeverity
    message: str
    compatibility: CompatibilityDirection = CompatibilityDirection.NONE
    provenance: ContractProvenance | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "severity": self.severity.value,
            "compatibility": self.compatibility.value,
            "message": self.message,
        }
        if self.provenance is not None:
            payload["provenance"] = self.provenance.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class ContractInspection:
    manifest_sha256: str
    sources: tuple[ContractSnapshot, ...]
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "apiVersion": CONTRACT_RESULT_API_VERSION,
            "kind": "ContractInspection",
            "manifestSha256": self.manifest_sha256,
            "sources": [source.to_dict() for source in self.sources],
            "sha256": self.sha256,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict()) + "\n"


@dataclass(frozen=True, slots=True)
class ContractCheckResult:
    snapshot: ContractSnapshot
    findings: tuple[ContractFinding, ...]
    sha256: str

    @property
    def valid(self) -> bool:
        return not any(item.severity is ContractSeverity.ERROR for item in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "apiVersion": CONTRACT_RESULT_API_VERSION,
            "kind": "ContractCheckResult",
            "snapshot": self.snapshot.to_dict(),
            "findings": [item.to_dict() for item in self.findings],
            "valid": self.valid,
            "sha256": self.sha256,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict()) + "\n"


@dataclass(frozen=True, slots=True)
class ContractDiffResult:
    before: ContractSnapshot
    after: ContractSnapshot
    direction: CompatibilityDirection
    findings: tuple[ContractFinding, ...]
    sha256: str

    @property
    def compatible(self) -> bool:
        return not any(item.severity is ContractSeverity.ERROR for item in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "apiVersion": CONTRACT_DIFF_API_VERSION,
            "kind": "ContractDiffResult",
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "direction": self.direction.value,
            "findings": [item.to_dict() for item in self.findings],
            "compatible": self.compatible,
            "sha256": self.sha256,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict()) + "\n"


class ContractFormatAdapter(Protocol):
    kind: str

    def check(self, snapshot: ContractSnapshot) -> Sequence[ContractFinding]: ...

    def diff(
        self,
        before: ContractSnapshot,
        after: ContractSnapshot,
        direction: CompatibilityDirection,
    ) -> Sequence[ContractFinding]: ...


class ContractAdapterRegistry:
    def __init__(self, adapters: Sequence[ContractFormatAdapter] = ()) -> None:
        self._adapters: dict[str, ContractFormatAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ContractFormatAdapter) -> None:
        kind = _contract_kind(adapter.kind)
        if kind in self._adapters:
            raise ContractError(
                "SDAI-CONTRACT-ADAPTER-002",
                f"multiple adapters are registered for contract kind '{kind}'",
            )
        self._adapters[kind] = adapter

    def resolve(self, kind: str) -> ContractFormatAdapter:
        normalized = _contract_kind(kind)
        adapter = self._adapters.get(normalized)
        if adapter is None:
            raise ContractError(
                "SDAI-CONTRACT-ADAPTER-001",
                f"no contract format adapter is registered for kind '{normalized}'",
            )
        return adapter

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate mapping key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "SDAI-CONTRACT-MODEL-001",
            "contract data must be canonical finite JSON",
        ) from exc


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_text(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ContractError("SDAI-CONTRACT-SOURCE-001", f"{label} must be a string-keyed mapping")
    return value


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ContractError("SDAI-CONTRACT-SOURCE-001", f"{label} must be a string")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not _IDENTIFIER.fullmatch(normalized):
        raise ContractError(
            "SDAI-CONTRACT-SOURCE-001",
            f"{label} '{normalized}' is not a portable lowercase identifier",
        )
    return normalized


def _contract_kind(value: object) -> str:
    kind = _identifier(value, label="contract kind")
    if kind not in SUPPORTED_CONTRACT_KINDS:
        raise ContractError(
            "SDAI-CONTRACT-SOURCE-004",
            f"unsupported contract kind '{kind}'",
        )
    return kind


def _portable_relative_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ContractError("SDAI-CONTRACT-SOURCE-002", f"{label} must be a portable relative path")
    if "\\" in value or "\x00" in value or re.match(r"^[A-Za-z]:", value):
        raise ContractError("SDAI-CONTRACT-SOURCE-002", f"{label} must be a portable relative path")
    normalized = unicodedata.normalize("NFC", value)
    path = PurePosixPath(normalized)
    parts = normalized.split("/")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ContractError("SDAI-CONTRACT-SOURCE-002", f"{label} must be a portable relative path")
    if len(normalized.encode("utf-8")) > 4096 or len(parts) > 64:
        raise ContractError("SDAI-CONTRACT-SOURCE-002", f"{label} exceeds portable path limits")
    for part in parts:
        if len(part.encode("utf-8")) > 255 or part != part.strip():
            raise ContractError("SDAI-CONTRACT-SOURCE-002", f"{label} contains a non-portable segment")
        if any(ord(char) < 32 for char in part):
            raise ContractError("SDAI-CONTRACT-SOURCE-002", f"{label} contains a control character")
        if any(char in _WINDOWS_FORBIDDEN for char in part) or part.endswith((".", " ")):
            raise ContractError("SDAI-CONTRACT-SOURCE-002", f"{label} is not portable across Windows/Linux")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise ContractError("SDAI-CONTRACT-SOURCE-002", f"{label} uses a reserved Windows segment")
    return path.as_posix()


def _read_bounded_utf8(path: Path, *, maximum: int, label: str) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(maximum + 1)
    except OSError as exc:
        raise ContractError("SDAI-CONTRACT-SOURCE-003", f"cannot read {label}: {path}") from exc
    if len(data) > maximum:
        raise ContractError(
            "SDAI-CONTRACT-SOURCE-006",
            f"{label} exceeds the {maximum}-byte limit: {path}",
        )
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(
            "SDAI-CONTRACT-SOURCE-005",
            f"{label} is not valid UTF-8 at byte {exc.start}: {path}",
        ) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _resolve_local_source(root: Path, relative_path: str, *, label: str) -> Path:
    root = root.resolve()
    candidate = root / relative_path
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ContractError("SDAI-CONTRACT-SOURCE-003", f"{label} does not exist: {relative_path}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ContractError(
            "SDAI-CONTRACT-SOURCE-007",
            f"{label} escapes the project workspace: {relative_path}",
        ) from exc
    current = root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise ContractError(
                "SDAI-CONTRACT-SOURCE-007",
                f"{label} must not traverse symbolic links: {relative_path}",
            )
    if not resolved.is_file():
        raise ContractError("SDAI-CONTRACT-SOURCE-003", f"{label} is not a file: {relative_path}")
    return resolved


def load_contract_sources(
    root: Path,
    manifest_path: str = CONTRACT_MANIFEST_PATH,
) -> tuple[tuple[ContractSource, ...], str]:
    root = root.resolve()
    relative_manifest = _portable_relative_path(manifest_path, label="contract manifest path")
    manifest = _resolve_local_source(root, relative_manifest, label="contract manifest")
    text = _read_bounded_utf8(
        manifest,
        maximum=CONTRACT_MANIFEST_MAX_BYTES,
        label="contract manifest",
    )
    try:
        parsed = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ContractError("SDAI-CONTRACT-SOURCE-001", "contract manifest is not valid YAML") from exc
    document = _mapping(parsed, label="contract manifest")
    allowed = {"apiVersion", "kind", "sources"}
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise ContractError(
            "SDAI-CONTRACT-SOURCE-001",
            "contract manifest contains unsupported field(s): " + ", ".join(unknown),
        )
    if document.get("apiVersion") != CONTRACT_SOURCE_API_VERSION or document.get("kind") != "ContractSources":
        raise ContractError(
            "SDAI-CONTRACT-SOURCE-001",
            f"contract manifest must use apiVersion={CONTRACT_SOURCE_API_VERSION} kind=ContractSources",
        )
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ContractError("SDAI-CONTRACT-SOURCE-001", "contract manifest sources must be a non-empty list")
    if len(raw_sources) > 1024:
        raise ContractError("SDAI-CONTRACT-SOURCE-006", "contract manifest exceeds the 1024-source limit")

    sources: list[ContractSource] = []
    identities: set[str] = set()
    paths: set[str] = set()
    for index, raw in enumerate(raw_sources):
        item = _mapping(raw, label=f"sources[{index}]")
        if set(item) != {"id", "kind", "path"}:
            missing = sorted({"id", "kind", "path"} - set(item))
            unknown_item = sorted(set(item) - {"id", "kind", "path"})
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if unknown_item:
                detail.append("unsupported " + ", ".join(unknown_item))
            raise ContractError("SDAI-CONTRACT-SOURCE-001", f"sources[{index}] has " + "; ".join(detail))
        source_id = _identifier(item["id"], label=f"sources[{index}].id")
        kind = _contract_kind(item["kind"])
        path = _portable_relative_path(item["path"], label=f"sources[{index}].path")
        if source_id in identities:
            raise ContractError("SDAI-CONTRACT-SOURCE-008", f"duplicate contract source id '{source_id}'")
        if path in paths:
            raise ContractError("SDAI-CONTRACT-SOURCE-009", f"contract source path '{path}' is declared more than once")
        identities.add(source_id)
        paths.add(path)
        sources.append(ContractSource(source_id=source_id, kind=kind, path=path))

    ordered = tuple(sorted(sources, key=lambda item: (item.source_id, item.kind, item.path)))
    canonical_manifest = {
        "apiVersion": CONTRACT_SOURCE_API_VERSION,
        "kind": "ContractSources",
        "sources": [item.to_dict() for item in ordered],
    }
    return ordered, _hash_json(canonical_manifest)


def load_contract_snapshot(root: Path, source: ContractSource) -> ContractSnapshot:
    path = _resolve_local_source(root.resolve(), source.path, label=f"contract source '{source.source_id}'")
    text = _read_bounded_utf8(
        path,
        maximum=CONTRACT_SOURCE_MAX_BYTES,
        label=f"contract source '{source.source_id}'",
    )
    encoded = text.encode("utf-8")
    return ContractSnapshot(
        source=source,
        sha256=_hash_text(text),
        size_bytes=len(encoded),
        text=text,
    )


def discover_contracts(root: Path, manifest_path: str = CONTRACT_MANIFEST_PATH) -> ContractInspection:
    sources, manifest_sha256 = load_contract_sources(root, manifest_path)
    snapshots = tuple(load_contract_snapshot(root, source) for source in sources)
    unsigned = {
        "apiVersion": CONTRACT_RESULT_API_VERSION,
        "kind": "ContractInspection",
        "manifestSha256": manifest_sha256,
        "sources": [snapshot.to_dict() for snapshot in snapshots],
    }
    return ContractInspection(
        manifest_sha256=manifest_sha256,
        sources=snapshots,
        sha256=_hash_json(unsigned),
    )


def find_contract_source(
    root: Path,
    source_id: str,
    manifest_path: str = CONTRACT_MANIFEST_PATH,
) -> ContractSnapshot:
    inspection = discover_contracts(root, manifest_path)
    normalized = _identifier(source_id, label="contract source id")
    for snapshot in inspection.sources:
        if snapshot.source.source_id == normalized:
            return snapshot
    raise ContractError("SDAI-CONTRACT-SOURCE-010", f"contract source '{normalized}' is not declared")


def check_contract(
    snapshot: ContractSnapshot,
    registry: ContractAdapterRegistry,
) -> ContractCheckResult:
    adapter = registry.resolve(snapshot.source.kind)
    findings = tuple(
        sorted(
            adapter.check(snapshot),
            key=lambda item: (
                item.severity.value,
                item.code,
                item.provenance.pointer if item.provenance and item.provenance.pointer else "",
                item.message,
            ),
        )
    )
    unsigned = {
        "apiVersion": CONTRACT_RESULT_API_VERSION,
        "kind": "ContractCheckResult",
        "snapshot": snapshot.to_dict(),
        "findings": [item.to_dict() for item in findings],
        "valid": not any(item.severity is ContractSeverity.ERROR for item in findings),
    }
    return ContractCheckResult(snapshot=snapshot, findings=findings, sha256=_hash_json(unsigned))


def diff_contracts(
    before: ContractSnapshot,
    after: ContractSnapshot,
    registry: ContractAdapterRegistry,
    direction: CompatibilityDirection = CompatibilityDirection.BACKWARD,
) -> ContractDiffResult:
    if before.source.kind != after.source.kind:
        raise ContractError(
            "SDAI-CONTRACT-DIFF-001",
            f"cannot diff contract kinds '{before.source.kind}' and '{after.source.kind}'",
        )
    adapter = registry.resolve(before.source.kind)
    findings = tuple(
        sorted(
            adapter.diff(before, after, direction),
            key=lambda item: (
                item.severity.value,
                item.code,
                item.provenance.pointer if item.provenance and item.provenance.pointer else "",
                item.message,
            ),
        )
    )
    unsigned = {
        "apiVersion": CONTRACT_DIFF_API_VERSION,
        "kind": "ContractDiffResult",
        "before": before.to_dict(),
        "after": after.to_dict(),
        "direction": direction.value,
        "findings": [item.to_dict() for item in findings],
        "compatible": not any(item.severity is ContractSeverity.ERROR for item in findings),
    }
    return ContractDiffResult(
        before=before,
        after=after,
        direction=direction,
        findings=findings,
        sha256=_hash_json(unsigned),
    )


def load_explicit_snapshot(
    root: Path,
    *,
    source_id: str,
    kind: str,
    path: str,
) -> ContractSnapshot:
    """Load an explicitly supplied local comparison source without discovery or network access."""
    source = ContractSource(
        source_id=_identifier(source_id, label="contract source id"),
        kind=_contract_kind(kind),
        path=_portable_relative_path(path, label="contract source path"),
    )
    return load_contract_snapshot(root, source)
