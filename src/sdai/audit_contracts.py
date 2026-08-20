from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from sdai.models import validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project


AUDIT_EVENT_API_VERSION = "sdai.audit-event/v1"
AUDIT_LEDGER_API_VERSION = "sdai.audit-ledger/v1"
AUDIT_EVENTS_RELATIVE_PATH = ".sdai/audit/events.jsonl"
AUDIT_MAX_EVENT_BYTES = 1024 * 1024
AUDIT_MAX_LEDGER_BYTES = 64 * 1024 * 1024
AUDIT_MAX_EVENTS = 100_000
AUDIT_MAX_JSON_DEPTH = 32
AUDIT_MAX_JSON_NODES = 50_000

_ZERO_HASH = "sha256:" + ("0" * 64)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SIMPLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,255}$")
_ACTION_KIND = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_BINDING_KINDS = frozenset(
    {
        "input",
        "context",
        "output",
        "artifact",
        "policy",
        "constitution",
        "workflow",
        "evidence",
        "trace",
        "quality",
        "security",
        "eval",
    }
)
_ACTOR_KINDS = frozenset({"human", "ai", "system", "workflow"})
_EVENT_CATEGORIES = frozenset({"human", "ai", "system", "workflow", "authority", "evidence"})
_EVENT_KEYS = frozenset(
    {
        "apiVersion",
        "sequence",
        "eventId",
        "featureId",
        "category",
        "occurredAt",
        "actor",
        "action",
        "execution",
        "bindings",
        "metadata",
        "previousSha256",
        "sha256",
    }
)
_SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "apikey",
        "privatekey",
        "clientsecret",
        "accesskey",
        "accesskeyid",
        "secretaccesskey",
        "authorization",
        "credential",
        "credentials",
        "cookie",
        "setcookie",
    }
)


class AuditProvenanceError(RuntimeError):
    """Raised when audit/provenance input or persistence is invalid or unsafe."""


def _fail(code: str, message: str) -> AuditProvenanceError:
    return AuditProvenanceError(f"{code}: {message}")


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _feature_id(value: object) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-AUDIT-002", "featureId must be a string")
    try:
        return validate_feature_id(value)
    except ValueError as exc:
        raise _fail("SDAI-AUDIT-002", f"invalid featureId: {value!r}") from exc


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-AUDIT-002", f"audit data is not finite canonical JSON: {exc}") from exc


def _normalize_secret_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _is_secret_key(value: str) -> bool:
    normalized = _normalize_secret_key(value)
    if normalized in _SECRET_KEYS:
        return True
    return any(
        normalized.endswith(suffix)
        for suffix in (
            "password",
            "passwd",
            "secret",
            "token",
            "apikey",
            "privatekey",
            "clientsecret",
            "accesskey",
            "authorization",
            "credential",
            "credentials",
        )
    )


def _freeze_json(
    value: object,
    *,
    label: str,
    depth: int = 0,
    counter: list[int] | None = None,
) -> object:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > AUDIT_MAX_JSON_NODES:
        raise _fail("SDAI-AUDIT-002", f"{label} exceeds the JSON node limit")
    if depth > AUDIT_MAX_JSON_DEPTH:
        raise _fail("SDAI-AUDIT-002", f"{label} exceeds the JSON depth limit")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail("SDAI-AUDIT-002", f"{label} contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, label=f"{label}[{index}]", depth=depth + 1, counter=counter)
            for index, item in enumerate(value)
        )
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise _fail("SDAI-AUDIT-002", f"{label} mapping keys must be strings")
        frozen: dict[str, object] = {}
        for key in sorted(value):
            if _is_secret_key(key):
                raise _fail(
                    "SDAI-AUDIT-003",
                    f"{label} contains reserved secret-bearing key {key!r}",
                )
            frozen[key] = _freeze_json(
                value[key],
                label=f"{label}.{key}",
                depth=depth + 1,
                counter=counter,
            )
        return MappingProxyType(frozen)
    raise _fail("SDAI-AUDIT-002", f"{label} contains unsupported type {type(value).__name__}")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _text(
    value: object,
    *,
    label: str,
    maximum: int,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise _fail("SDAI-AUDIT-002", f"{label} must be a string")
    if value != value.strip() or not value or len(value.encode("utf-8")) > maximum:
        raise _fail("SDAI-AUDIT-002", f"{label} must be non-empty bounded text without surrounding whitespace")
    if any(ord(char) < 32 and char not in {"\t"} for char in value) or "\x7f" in value:
        raise _fail("SDAI-AUDIT-002", f"{label} contains control characters")
    return value


def _simple_id(value: object, *, label: str, optional: bool = False) -> str | None:
    text = _text(value, label=label, maximum=256, optional=optional)
    if text is None:
        return None
    if _SIMPLE_ID.fullmatch(text) is None:
        raise _fail("SDAI-AUDIT-002", f"{label} must be a safe portable identifier")
    return text


def _reference(value: object, *, label: str) -> str:
    text = _text(value, label=label, maximum=512)
    assert text is not None
    if "\\" in text or "\x00" in text:
        raise _fail("SDAI-AUDIT-002", f"{label} must use portable reference syntax")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise _fail("SDAI-AUDIT-002", f"{label} must not be an absolute filesystem path")
    path_like = text.split("/")
    if any(part in {"", ".", ".."} for part in path_like):
        raise _fail("SDAI-AUDIT-002", f"{label} contains an unsafe path segment")
    return text


def _timestamp(value: object) -> str:
    text = _text(value, label="occurredAt", maximum=128)
    assert text is not None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _fail("SDAI-AUDIT-002", f"occurredAt is not ISO-8601: {text!r}") from exc
    if parsed.tzinfo is None:
        raise _fail("SDAI-AUDIT-002", "occurredAt must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _git_commit(value: object, *, optional: bool = True) -> str | None:
    text = _text(value, label="execution.gitCommit", maximum=64, optional=optional)
    if text is None:
        return None
    normalized = text.casefold()
    if _GIT_COMMIT.fullmatch(normalized) is None:
        raise _fail("SDAI-AUDIT-002", f"invalid Git commit identity: {text!r}")
    return normalized


def _safe_component_chain(root: Path, candidate: Path, *, label: str) -> Path:
    try:
        safe = ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-AUDIT-004", f"{label} escapes the project workspace") from exc
    relative = safe.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-AUDIT-004", f"{label} contains a symlink component")
    return safe


def _legacy_is_execution_only(legacy: Path) -> bool:
    """Return True only when a legacy feature directory contains durable execution state.

    `.sdai/execution` is runtime evidence, not specification/audit authority. Any other
    legacy entry remains authoritative and therefore ambiguous beside a modern feature.
    """
    if legacy.is_symlink() or not legacy.is_dir():
        return False
    try:
        top_level = tuple(legacy.iterdir())
    except OSError:
        return False
    if len(top_level) != 1 or top_level[0].name != ".sdai":
        return False
    sdai = top_level[0]
    if sdai.is_symlink() or not sdai.is_dir():
        return False
    try:
        state_entries = tuple(sdai.iterdir())
    except OSError:
        return False
    if len(state_entries) != 1 or state_entries[0].name != "execution":
        return False
    execution = state_entries[0]
    return execution.is_dir() and not execution.is_symlink()


def _feature_workspace(project_root: Path, feature_id: str) -> Path:
    root = project_root.resolve()
    feature = _feature_id(feature_id)
    modern = _safe_component_chain(root, root / "specs" / "changes" / feature, label="feature workspace")
    legacy = _safe_component_chain(root, root / "specs" / feature, label="legacy feature workspace")
    modern_exists = modern.exists()
    legacy_exists = legacy.exists()
    if modern_exists and legacy_exists and not _legacy_is_execution_only(legacy):
        raise _fail(
            "SDAI-AUDIT-004",
            f"feature {feature!r} has both current and legacy workspaces; audit authority is ambiguous",
        )
    workspace = modern if modern_exists else legacy if legacy_exists else None
    if workspace is None:
        raise _fail("SDAI-AUDIT-004", f"feature workspace does not exist for {feature!r}")
    if workspace.is_symlink() or not workspace.is_dir():
        raise _fail("SDAI-AUDIT-004", "feature workspace must be a regular non-symlink directory")
    return workspace


__all__ = [
    "AUDIT_EVENT_API_VERSION",
    "AUDIT_LEDGER_API_VERSION",
    "AUDIT_EVENTS_RELATIVE_PATH",
    "AuditProvenanceError",
]