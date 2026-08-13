from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.trace_graph import TraceProvenance


TRACE_EVIDENCE_API_VERSION = "sdai.trace-evidence/v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+\-]{0,255}$")
_SEMANTIC_ROLE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_TEXT = 4096
_MAX_RESULT_DEPTH = 16
_MAX_RESULT_ITEMS = 4096


class TraceEvidenceError(RuntimeError):
    """Raised when canonical trace evidence is malformed, ambiguous, or unsafe."""


class EvidenceKind(str, Enum):
    EXECUTION = "execution"
    TEST = "test"
    QUALITY = "quality"
    SECURITY = "security"
    APPROVAL = "approval"
    REVIEW = "review"
    OPERATIONAL = "operational"


class EvidenceStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    RECORDED = "recorded"


class EvidenceBindingKind(str, Enum):
    ARTIFACT = "artifact"
    SOURCE = "source"
    TEST = "test"
    EVIDENCE = "evidence"


def _fail(code: str, message: str) -> TraceEvidenceError:
    return TraceEvidenceError(f"{code}: {message}")


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    _validate_json_value(payload, label="trace evidence")
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-EVIDENCE-001", f"trace evidence is not canonical JSON: {exc}") from exc


def _validate_json_value(
    value: object,
    *,
    label: str,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > _MAX_RESULT_ITEMS:
        raise _fail("SDAI-EVIDENCE-001", f"{label} exceeds the finite JSON item limit")
    if depth > _MAX_RESULT_DEPTH:
        raise _fail("SDAI-EVIDENCE-001", f"{label} exceeds the finite JSON nesting limit")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail("SDAI-EVIDENCE-001", f"{label} contains a non-finite number")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(
                item,
                label=f"{label}[{index}]",
                depth=depth + 1,
                counter=counter,
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise _fail(
                    "SDAI-EVIDENCE-001",
                    f"{label} mapping keys must be non-empty strings",
                )
            _validate_json_value(
                item,
                label=f"{label}.{key}",
                depth=depth + 1,
                counter=counter,
            )
        return
    raise _fail(
        "SDAI-EVIDENCE-001",
        f"{label} contains unsupported JSON type {type(value).__name__}",
    )


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _normalize_json_mapping(value: Mapping[str, object] | None, *, label: str) -> Mapping[str, object]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise _fail("SDAI-EVIDENCE-001", f"{label} must be a mapping")
    normalized = dict(value)
    _validate_json_value(normalized, label=label)
    clone = json.loads(
        json.dumps(
            normalized,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    if not isinstance(clone, dict):
        raise _fail("SDAI-EVIDENCE-001", f"{label} must normalize to a mapping")
    frozen = _freeze_json(clone)
    if not isinstance(frozen, Mapping):
        raise _fail("SDAI-EVIDENCE-001", f"{label} must normalize to a mapping")
    return frozen


def _mapping_dict(value: Mapping[str, object]) -> dict[str, object]:
    thawed = _thaw_json(value)
    if not isinstance(thawed, dict):
        raise _fail("SDAI-EVIDENCE-001", "trace evidence mapping did not normalize to an object")
    return thawed


def _normalize_text(value: str | None, *, label: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise _fail("SDAI-EVIDENCE-001", f"{label} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-EVIDENCE-001", f"{label} must be non-empty text")
    normalized = value.strip()
    if len(normalized) > _MAX_TEXT or any(ord(char) < 32 and char not in "\t\n" for char in normalized):
        raise _fail("SDAI-EVIDENCE-001", f"{label} is invalid or too long")
    return normalized


def _normalize_evidence_id(value: str) -> str:
    if not isinstance(value, str) or not _EVIDENCE_ID.fullmatch(value):
        raise _fail(
            "SDAI-EVIDENCE-001",
            "evidence_id must use 1-256 portable letters, numbers, '.', '_', ':', '/', '#', '@', '+', or '-'",
        )
    if "\\" in value or any(ord(char) < 32 for char in value):
        raise _fail("SDAI-EVIDENCE-001", f"evidence_id is not portable: {value!r}")
    return value


def _normalize_commit(value: str) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-EVIDENCE-002", "git_commit must be a string")
    normalized = value.strip().casefold()
    if not _GIT_COMMIT.fullmatch(normalized):
        raise _fail("SDAI-EVIDENCE-002", f"invalid Git commit identity: {value!r}")
    return normalized


def _normalize_source(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-EVIDENCE-002", "binding source must be a non-empty string")
    source = value.strip()
    if "\\" in source or source.startswith("/") or re.match(r"^[A-Za-z]:", source):
        raise _fail(
            "SDAI-EVIDENCE-002",
            f"binding source must be a repository-relative POSIX path: {value!r}",
        )
    path = PurePosixPath(source)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise _fail("SDAI-EVIDENCE-002", f"unsafe binding source path: {value!r}")
    return path.as_posix()


def _normalize_command(value: Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _fail(
            "SDAI-EVIDENCE-003",
            "command must be an executable/argument array, never a shell command string",
        )
    command: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item or "\x00" in item:
            raise _fail("SDAI-EVIDENCE-003", f"command[{index}] must be non-empty text")
        if len(item) > _MAX_TEXT:
            raise _fail("SDAI-EVIDENCE-003", f"command[{index}] is too long")
        command.append(item)
    if not command:
        raise _fail("SDAI-EVIDENCE-003", "command array must contain an executable")
    return tuple(command)


def _canonical_provenance(values: Sequence[TraceProvenance]) -> tuple[TraceProvenance, ...]:
    by_location: dict[tuple[str, int], TraceProvenance] = {}
    for item in values:
        if not isinstance(item, TraceProvenance):
            raise _fail("SDAI-EVIDENCE-004", "evidence provenance contains an invalid item")
        existing = by_location.get(item.location)
        if existing is not None and existing != item:
            raise _fail(
                "SDAI-EVIDENCE-004",
                f"conflicting evidence provenance at {item.source}:{item.line}",
            )
        by_location[item.location] = item
    if not by_location:
        raise _fail("SDAI-EVIDENCE-004", "evidence requires source/line provenance")
    return tuple(
        sorted(
            by_location.values(),
            key=lambda item: (
                item.source.casefold(),
                item.source,
                item.line,
                item.declaration_sha256 or "",
                item.detail or "",
            ),
        )
    )


@dataclass(frozen=True)
class EvidenceBinding:
    kind: EvidenceBindingKind
    source: str
    sha256: str

    def __post_init__(self) -> None:
        try:
            kind = self.kind if isinstance(self.kind, EvidenceBindingKind) else EvidenceBindingKind(self.kind)
        except ValueError as exc:
            raise _fail("SDAI-EVIDENCE-002", f"unsupported evidence binding kind: {self.kind!r}") from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source", _normalize_source(self.source))
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise _fail("SDAI-EVIDENCE-002", f"invalid SHA-256 binding: {self.sha256!r}")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "source": self.source, "sha256": self.sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "EvidenceBinding":
        if not isinstance(value, Mapping) or set(value) != {"kind", "source", "sha256"}:
            raise _fail(
                "SDAI-EVIDENCE-002",
                "evidence binding must contain exactly kind/source/sha256",
            )
        kind = value["kind"]
        source = value["source"]
        digest = value["sha256"]
        if not isinstance(kind, str) or not isinstance(source, str) or not isinstance(digest, str):
            raise _fail("SDAI-EVIDENCE-002", "evidence binding fields must be strings")
        return cls(kind, source, digest)


@dataclass(frozen=True)
class EvidenceProducer:
    semantic_role: str
    provider: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.semantic_role, str) or not _SEMANTIC_ROLE.fullmatch(self.semantic_role):
            raise _fail(
                "SDAI-EVIDENCE-005",
                "producer semantic_role must be a portable lowercase semantic role identifier",
            )
        object.__setattr__(self, "provider", _normalize_text(self.provider, label="producer provider"))
        object.__setattr__(self, "model", _normalize_text(self.model, label="producer model"))

    def as_dict(self) -> dict[str, object]:
        return {
            "semantic_role": self.semantic_role,
            "provider": self.provider,
            "model": self.model,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "EvidenceProducer":
        if not isinstance(value, Mapping) or set(value) != {"semantic_role", "provider", "model"}:
            raise _fail(
                "SDAI-EVIDENCE-005",
                "producer fields must contain exactly semantic_role/provider/model",
            )
        semantic_role = value["semantic_role"]
        provider = value["provider"]
        model = value["model"]
        if not isinstance(semantic_role, str):
            raise _fail("SDAI-EVIDENCE-005", "producer semantic_role must be a string")
        if provider is not None and not isinstance(provider, str):
            raise _fail("SDAI-EVIDENCE-005", "producer provider must be a string or null")
        if model is not None and not isinstance(model, str):
            raise _fail("SDAI-EVIDENCE-005", "producer model must be a string or null")
        return cls(semantic_role, provider, model)


@dataclass(frozen=True)
class TraceEvidence:
    evidence_id: str
    kind: EvidenceKind
    status: EvidenceStatus
    subject: str
    git_commit: str
    bindings: tuple[EvidenceBinding, ...]
    provenance: tuple[TraceProvenance, ...]
    producer: EvidenceProducer
    result: Mapping[str, object] | None = None
    command: Sequence[str] | None = None
    tool: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _normalize_evidence_id(self.evidence_id))
        try:
            kind = self.kind if isinstance(self.kind, EvidenceKind) else EvidenceKind(self.kind)
        except ValueError as exc:
            raise _fail("SDAI-EVIDENCE-001", f"unsupported evidence kind: {self.kind!r}") from exc
        try:
            status = self.status if isinstance(self.status, EvidenceStatus) else EvidenceStatus(self.status)
        except ValueError as exc:
            raise _fail("SDAI-EVIDENCE-001", f"unsupported evidence status: {self.status!r}") from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "status", status)
        subject = _normalize_text(self.subject, label="subject", required=True)
        assert subject is not None
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "git_commit", _normalize_commit(self.git_commit))
        if not isinstance(self.producer, EvidenceProducer):
            raise _fail("SDAI-EVIDENCE-005", "producer must be EvidenceProducer")
        bindings = tuple(self.bindings)
        if not bindings:
            raise _fail(
                "SDAI-EVIDENCE-002",
                "canonical trace evidence requires at least one SHA-256 content binding",
            )
        binding_map: dict[tuple[EvidenceBindingKind, str], EvidenceBinding] = {}
        for binding in bindings:
            if not isinstance(binding, EvidenceBinding):
                raise _fail("SDAI-EVIDENCE-002", "bindings contain an invalid item")
            key = (binding.kind, binding.source)
            existing = binding_map.get(key)
            if existing is not None and existing != binding:
                raise _fail(
                    "SDAI-EVIDENCE-002",
                    f"conflicting binding for {binding.kind.value}:{binding.source}",
                )
            binding_map[key] = binding
        object.__setattr__(
            self,
            "bindings",
            tuple(sorted(binding_map.values(), key=lambda item: (item.kind.value, item.source.casefold(), item.source, item.sha256))),
        )
        object.__setattr__(self, "provenance", _canonical_provenance(self.provenance))
        object.__setattr__(self, "result", _normalize_json_mapping(self.result, label="result"))
        object.__setattr__(self, "command", _normalize_command(self.command))
        object.__setattr__(self, "tool", _normalize_text(self.tool, label="tool"))

    def truth_dict(self) -> dict[str, object]:
        """Provider-independent claim used by trace coverage and freshness logic."""

        return {
            "apiVersion": TRACE_EVIDENCE_API_VERSION,
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "subject": self.subject,
            "git_commit": self.git_commit,
            "bindings": [item.as_dict() for item in self.bindings],
            "command": list(self.command),
            "tool": self.tool,
            "result": _mapping_dict(self.result),
            "provenance": [item.as_dict() for item in self.provenance],
        }

    @property
    def truth_sha256(self) -> str:
        return _sha256_bytes(_canonical_bytes(self.truth_dict()))

    def body_dict(self) -> dict[str, object]:
        body = self.truth_dict()
        body["producer"] = self.producer.as_dict()
        body["truth_sha256"] = self.truth_sha256
        return body

    @property
    def sha256(self) -> str:
        return _sha256_bytes(_canonical_bytes(self.body_dict()))

    def as_dict(self) -> dict[str, object]:
        payload = self.body_dict()
        payload["sha256"] = self.sha256
        return payload

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8")

    @classmethod
    def from_mapping(cls, value: object) -> "TraceEvidence":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-EVIDENCE-006", "trace evidence must be a mapping")
        required = {
            "apiVersion",
            "evidence_id",
            "kind",
            "status",
            "subject",
            "git_commit",
            "bindings",
            "command",
            "tool",
            "result",
            "provenance",
            "producer",
            "truth_sha256",
            "sha256",
        }
        if set(value) != required:
            raise _fail(
                "SDAI-EVIDENCE-006",
                "trace evidence fields do not match sdai.trace-evidence/v1",
            )
        if value["apiVersion"] != TRACE_EVIDENCE_API_VERSION:
            raise _fail("SDAI-EVIDENCE-006", "unsupported trace evidence apiVersion")
        evidence_id = value["evidence_id"]
        kind = value["kind"]
        status = value["status"]
        subject = value["subject"]
        git_commit = value["git_commit"]
        if not all(isinstance(item, str) for item in (evidence_id, kind, status, subject, git_commit)):
            raise _fail("SDAI-EVIDENCE-006", "identity/kind/status/subject/commit fields must be strings")
        raw_bindings = value["bindings"]
        raw_command = value["command"]
        raw_result = value["result"]
        raw_provenance = value["provenance"]
        if not isinstance(raw_bindings, list):
            raise _fail("SDAI-EVIDENCE-006", "bindings must be a list")
        if not isinstance(raw_command, list):
            raise _fail("SDAI-EVIDENCE-006", "command must be a list")
        if not isinstance(raw_result, Mapping):
            raise _fail("SDAI-EVIDENCE-006", "result must be a mapping")
        if not isinstance(raw_provenance, list):
            raise _fail("SDAI-EVIDENCE-006", "provenance must be a list")
        raw_tool = value["tool"]
        if raw_tool is not None and not isinstance(raw_tool, str):
            raise _fail("SDAI-EVIDENCE-006", "tool must be a string or null")
        record = cls(
            evidence_id=evidence_id,
            kind=kind,
            status=status,
            subject=subject,
            git_commit=git_commit,
            bindings=tuple(EvidenceBinding.from_mapping(item) for item in raw_bindings),
            command=tuple(raw_command),
            tool=raw_tool,
            result=dict(raw_result),
            provenance=tuple(TraceProvenance.from_mapping(item) for item in raw_provenance),
            producer=EvidenceProducer.from_mapping(value["producer"]),
        )
        supplied_truth = value["truth_sha256"]
        supplied_sha = value["sha256"]
        if not isinstance(supplied_truth, str) or not _SHA256.fullmatch(supplied_truth):
            raise _fail("SDAI-EVIDENCE-006", "truth_sha256 is invalid")
        if not isinstance(supplied_sha, str) or not _SHA256.fullmatch(supplied_sha):
            raise _fail("SDAI-EVIDENCE-006", "sha256 is invalid")
        if supplied_truth != record.truth_sha256:
            raise _fail("SDAI-EVIDENCE-006", "trace evidence truth SHA-256 does not match canonical claim")
        if supplied_sha != record.sha256:
            raise _fail("SDAI-EVIDENCE-006", "trace evidence SHA-256 does not match canonical record")
        return record

    @classmethod
    def from_json(cls, value: str | bytes) -> "TraceEvidence":
        try:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            raw = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise _fail("SDAI-EVIDENCE-006", f"invalid trace evidence JSON: {exc}") from exc
        return cls.from_mapping(raw)


def validate_trace_evidence(value: TraceEvidence | Mapping[str, object] | str | bytes) -> TraceEvidence:
    """Validate one canonical evidence record for ledger/gate/integration consumers."""

    if isinstance(value, TraceEvidence):
        return TraceEvidence.from_mapping(value.as_dict())
    if isinstance(value, Mapping):
        return TraceEvidence.from_mapping(value)
    if isinstance(value, (str, bytes)):
        return TraceEvidence.from_json(value)
    raise _fail("SDAI-EVIDENCE-006", f"unsupported evidence value type: {type(value).__name__}")


def load_trace_evidence(project_root: Path, path: Path) -> TraceEvidence:
    """Load UTF-8 evidence from a contained, non-symlink repository path."""

    root = project_root.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        safe = ensure_within_project(root, candidate, label="trace evidence")
    except PathSafetyError as exc:
        raise _fail("SDAI-EVIDENCE-007", "trace evidence path must stay inside the project root") from exc
    try:
        relative = safe.relative_to(root)
    except ValueError as exc:
        raise _fail("SDAI-EVIDENCE-007", "trace evidence path must stay inside the project root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise _fail(
                "SDAI-EVIDENCE-007",
                f"trace evidence path contains a symlink component: {relative.as_posix()}",
            )
    if safe.is_symlink() or not safe.is_file():
        raise _fail(
            "SDAI-EVIDENCE-007",
            f"trace evidence must be a regular non-symlink file: {relative.as_posix()}",
        )
    try:
        content = safe.read_bytes()
    except OSError as exc:
        raise _fail("SDAI-EVIDENCE-007", f"unable to read trace evidence: {exc}") from exc
    return TraceEvidence.from_json(content)
