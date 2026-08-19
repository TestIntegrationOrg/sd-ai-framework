from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Mapping

from sdai.agent_platform.model_routing import MODEL_ROUTING_API_VERSION, RoutingDecision
from sdai.models import FeatureContext, validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project


ROUTING_DIAGNOSTIC_API_VERSION = "sdai.routing-diagnostic/v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class RoutingDiagnosticError(RuntimeError):
    """Raised when routing-decision diagnostics are unsafe, corrupt, or mismatched."""


def _fail(code: str, message: str) -> RoutingDiagnosticError:
    return RoutingDiagnosticError(f"{code}: {message}")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-ROUTING-DIAG-001", "routing diagnostic is not canonical JSON") from exc


def _sha(value: object) -> str:
    return "sha256:" + sha256(_canonical_bytes(value)).hexdigest()


def _sha_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _safe(root: Path, candidate: Path, *, label: str) -> Path:
    try:
        safe = ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-ROUTING-DIAG-002", f"{label} escapes project root") from exc
    resolved_root = root.resolve()
    current = resolved_root
    try:
        relative = safe.relative_to(resolved_root)
    except ValueError:
        relative = safe.resolve(strict=False).relative_to(resolved_root)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-ROUTING-DIAG-002", f"{label} contains a symlink component")
    return safe


def _decision_from_json(serialized: str) -> dict[str, object]:
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise _fail("SDAI-ROUTING-DIAG-003", "routing decision JSON is invalid") from exc
    if not isinstance(payload, dict) or payload.get("apiVersion") != MODEL_ROUTING_API_VERSION:
        raise _fail("SDAI-ROUTING-DIAG-003", "routing decision has unsupported API version")
    claimed = payload.get("sha256")
    body = dict(payload)
    body.pop("sha256", None)
    expected = _sha(body)
    if not isinstance(claimed, str) or claimed != expected:
        raise _fail("SDAI-ROUTING-DIAG-003", "routing decision SHA-256 is invalid")
    return payload


def routing_decision_document_sha256(decision: RoutingDecision) -> str:
    """Return the exact serialized routing-document hash used by provider diagnostics."""
    if not isinstance(decision, RoutingDecision):
        raise TypeError("decision must be a RoutingDecision")
    serialized = decision.to_json()
    parsed = _decision_from_json(serialized)
    if parsed.get("sha256") != decision.sha256:
        raise _fail("SDAI-ROUTING-DIAG-003", "routing decision identity is inconsistent")
    return _sha_bytes(serialized.encode("utf-8"))


def _document_body(
    feature_id: str,
    decision: Mapping[str, object],
    *,
    routing_document_sha256: str,
    decision_sha256: str,
) -> dict[str, object]:
    return {
        "apiVersion": ROUTING_DIAGNOSTIC_API_VERSION,
        "featureId": feature_id,
        "routingApiVersion": MODEL_ROUTING_API_VERSION,
        "routingDecisionDocumentSha256": routing_document_sha256,
        "routingDecisionSha256": decision_sha256,
        "decision": dict(decision),
    }


def persist_routing_decision(
    project_root: Path,
    feature_id: str,
    decision: RoutingDecision,
) -> Path | None:
    """Persist one immutable privacy-safe routing decision before provider execution.

    Provider diagnostics already bind the SHA-256 of the exact serialized routing
    document. Persist by that same identity so historical execution evidence can be
    joined without recomputing or guessing a route. The decision's own canonical-body
    SHA is stored separately inside the immutable diagnostic document.
    """
    if not isinstance(decision, RoutingDecision):
        raise TypeError("decision must be a RoutingDecision")
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    workspace = FeatureContext(root, feature).feature_dir
    if not workspace.exists() and not workspace.is_symlink():
        return None
    if workspace.is_symlink() or not workspace.is_dir():
        raise _fail("SDAI-ROUTING-DIAG-002", "feature workspace is missing or unsafe")
    _safe(root, workspace, label="routing diagnostic feature workspace")

    serialized = decision.to_json()
    parsed = _decision_from_json(serialized)
    if parsed.get("selected_profile") != decision.selected_profile:
        raise _fail("SDAI-ROUTING-DIAG-003", "routing decision selected profile is inconsistent")
    decision_sha = decision.sha256
    if _SHA256.fullmatch(decision_sha) is None or parsed.get("sha256") != decision_sha:
        raise _fail("SDAI-ROUTING-DIAG-003", "routing decision SHA-256 is invalid")
    routing_document_sha = _sha_bytes(serialized.encode("utf-8"))

    directory = _safe(
        root,
        workspace / ".sdai" / "diagnostics" / "routing",
        label="routing diagnostic directory",
    )
    directory.mkdir(parents=True, exist_ok=True)
    _safe(root, directory, label="routing diagnostic directory")
    path = _safe(
        root,
        directory / f"{routing_document_sha.removeprefix('sha256:')}.json",
        label="routing diagnostic file",
    )
    body = _document_body(
        feature,
        parsed,
        routing_document_sha256=routing_document_sha,
        decision_sha256=decision_sha,
    )
    document = dict(body)
    document["documentSha256"] = _sha(body)
    data = _canonical_bytes(document) + b"\n"
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise _fail("SDAI-ROUTING-DIAG-004", "existing routing diagnostic conflicts with decision")
        return path
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if not path.is_file() or path.read_bytes() != data:
            raise _fail("SDAI-ROUTING-DIAG-004", "concurrent routing diagnostic conflicts with decision")
    except OSError as exc:
        raise _fail("SDAI-ROUTING-DIAG-004", "unable to persist routing diagnostic") from exc
    return path


def load_routing_diagnostic(
    project_root: Path,
    feature_id: str,
    routing_document_sha256: str,
) -> dict[str, object] | None:
    """Load and integrity-verify one immutable routing diagnostic by document hash."""
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    if not isinstance(routing_document_sha256, str) or _SHA256.fullmatch(routing_document_sha256) is None:
        raise _fail("SDAI-ROUTING-DIAG-001", "routing document SHA-256 is invalid")
    workspace = FeatureContext(root, feature).feature_dir
    if not workspace.exists():
        return None
    path = _safe(
        root,
        workspace
        / ".sdai"
        / "diagnostics"
        / "routing"
        / f"{routing_document_sha256.removeprefix('sha256:')}.json",
        label="routing diagnostic file",
    )
    if not path.exists():
        return None
    if not path.is_file():
        raise _fail("SDAI-ROUTING-DIAG-002", "routing diagnostic path is not a regular file")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("SDAI-ROUTING-DIAG-003", "routing diagnostic is invalid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise _fail("SDAI-ROUTING-DIAG-003", "routing diagnostic must be a JSON object")
    if raw != _canonical_bytes(payload) + b"\n":
        raise _fail("SDAI-ROUTING-DIAG-003", "routing diagnostic bytes are not canonical")
    if payload.get("apiVersion") != ROUTING_DIAGNOSTIC_API_VERSION:
        raise _fail("SDAI-ROUTING-DIAG-003", "routing diagnostic has unsupported API version")
    if payload.get("featureId") != feature or payload.get("routingApiVersion") != MODEL_ROUTING_API_VERSION:
        raise _fail("SDAI-ROUTING-DIAG-003", "routing diagnostic identity does not match request")
    if payload.get("routingDecisionDocumentSha256") != routing_document_sha256:
        raise _fail("SDAI-ROUTING-DIAG-003", "routing diagnostic document identity mismatch")
    document_sha = payload.get("documentSha256")
    body = dict(payload)
    body.pop("documentSha256", None)
    if not isinstance(document_sha, str) or document_sha != _sha(body):
        raise _fail("SDAI-ROUTING-DIAG-003", "routing diagnostic document SHA-256 is invalid")
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        raise _fail("SDAI-ROUTING-DIAG-003", "routing diagnostic decision is missing")
    serialized_decision = _canonical_bytes(decision).decode("utf-8")
    parsed = _decision_from_json(serialized_decision)
    decision_sha = parsed.get("sha256")
    if decision_sha != payload.get("routingDecisionSha256"):
        raise _fail("SDAI-ROUTING-DIAG-003", "routing decision SHA-256 mismatch")
    if _sha_bytes(serialized_decision.encode("utf-8")) != routing_document_sha256:
        raise _fail("SDAI-ROUTING-DIAG-003", "routing decision document SHA-256 mismatch")
    return payload


__all__ = [
    "ROUTING_DIAGNOSTIC_API_VERSION",
    "RoutingDiagnosticError",
    "load_routing_diagnostic",
    "persist_routing_decision",
    "routing_decision_document_sha256",
]
