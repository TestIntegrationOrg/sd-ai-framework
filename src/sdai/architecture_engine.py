from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

import yaml

from sdai.architecture_communication_observer import ServiceCommunicationObserver
from sdai.architecture_data_observer import RepositoryDataObserver
from sdai.architecture_dependency_observer import DependencyImportObserver
from sdai.architecture_deployment_observer import DeploymentTopologyObserver
from sdai.architecture_drift import (
    ApprovedArchitecture,
    ArchitectureDriftError,
    ArchitectureDriftFinding,
    ArchitectureDriftReport,
    ArchitectureDriftSeverity,
    ArchitectureObservation,
    load_approved_architecture,
)
from sdai.architecture_security_drift import evaluate_trust_boundary_security
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.structured_contracts import UniqueKeySafeLoader, normalize_structured_json


ARCHITECTURE_CHECK_API_VERSION = "sdai.architecture-check/v1"
ARCHITECTURE_POLICY_API_VERSION = "sdai.architecture-policy/v1"
ARCHITECTURE_POLICY_PATH = ".sdai/architecture-policy.yaml"
ARCHITECTURE_POLICY_MAX_BYTES = 1024 * 1024
ARCHITECTURE_MAX_POLICY_CODES = 10_000
ARCHITECTURE_BLOCKED_EXIT_CODE = 2


class ArchitectureEngineError(ArchitectureDriftError):
    """Raised when unified architecture orchestration/policy cannot be evaluated safely."""


def _fail(code: str, message: str) -> ArchitectureEngineError:
    return ArchitectureEngineError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-ARCH-ENGINE-001", f"architecture result is not canonical JSON: {exc}") from exc


def _hash(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _severity(value: str) -> ArchitectureDriftSeverity:
    try:
        return ArchitectureDriftSeverity(value)
    except ValueError as exc:
        raise _fail("SDAI-ARCH-POLICY-002", f"unsupported architecture severity {value!r}") from exc


def _bounded_codes(value: object, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > ARCHITECTURE_MAX_POLICY_CODES:
        raise _fail("SDAI-ARCH-POLICY-002", f"{label} must be a bounded list")
    codes: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 160 or not item.startswith("ARCH-"):
            raise _fail("SDAI-ARCH-POLICY-002", f"{label} contains an invalid finding code")
        codes.add(item)
    return tuple(sorted(codes))


@dataclass(frozen=True, slots=True)
class ArchitecturePolicyLayer:
    layer_id: str
    block_severities: tuple[ArchitectureDriftSeverity, ...] = ()
    block_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.layer_id, str) or not self.layer_id or len(self.layer_id) > 128:
            raise _fail("SDAI-ARCH-POLICY-001", "architecture policy layer id must be bounded text")
        severities = tuple(sorted(set(self.block_severities), key=lambda item: item.value))
        if any(not isinstance(item, ArchitectureDriftSeverity) for item in severities):
            raise _fail("SDAI-ARCH-POLICY-001", "architecture policy severities must use ArchitectureDriftSeverity")
        codes = tuple(sorted(set(self.block_codes)))
        if any(not isinstance(code, str) or not code.startswith("ARCH-") for code in codes):
            raise _fail("SDAI-ARCH-POLICY-001", "architecture policy finding codes are invalid")
        object.__setattr__(self, "block_severities", severities)
        object.__setattr__(self, "block_codes", codes)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.layer_id,
            "blockSeverities": [item.value for item in self.block_severities],
            "blockCodes": list(self.block_codes),
        }


CORE_ARCHITECTURE_POLICY = ArchitecturePolicyLayer(
    "core",
    block_severities=(ArchitectureDriftSeverity.ERROR,),
)


@dataclass(frozen=True, slots=True)
class EffectiveArchitecturePolicy:
    layers: tuple[ArchitecturePolicyLayer, ...]
    block_severities: tuple[ArchitectureDriftSeverity, ...]
    block_codes: tuple[str, ...]

    @property
    def sha256(self) -> str:
        return _hash(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "apiVersion": ARCHITECTURE_POLICY_API_VERSION,
            "kind": "EffectiveArchitecturePolicy",
            "layers": [layer.to_dict() for layer in self.layers],
            "blockSeverities": [item.value for item in self.block_severities],
            "blockCodes": list(self.block_codes),
        }

    def blocks(self, finding: ArchitectureDriftFinding) -> bool:
        return finding.severity in self.block_severities or finding.code in self.block_codes


def resolve_architecture_policy(
    organization: ArchitecturePolicyLayer | None = None,
    project: ArchitecturePolicyLayer | None = None,
) -> EffectiveArchitecturePolicy:
    """Resolve monotonic core -> organization -> project architecture policy.

    Layers are additive only. There is intentionally no allow/downgrade primitive in v1,
    so lower precedence configuration cannot weaken core or organization authority.
    """
    layers = tuple(layer for layer in (CORE_ARCHITECTURE_POLICY, organization, project) if layer is not None)
    severities = tuple(
        sorted({severity for layer in layers for severity in layer.block_severities}, key=lambda item: item.value)
    )
    codes = tuple(sorted({code for layer in layers for code in layer.block_codes}))
    return EffectiveArchitecturePolicy(layers, severities, codes)


def _policy_file(project_root: Path) -> Path | None:
    root = project_root.resolve()
    path = root / ARCHITECTURE_POLICY_PATH
    if not path.exists():
        return None
    try:
        safe = ensure_within_project(root, path, label="architecture policy")
    except PathSafetyError as exc:
        raise _fail("SDAI-ARCH-POLICY-003", "architecture policy escapes project workspace") from exc
    if safe.is_symlink() or not safe.is_file():
        raise _fail("SDAI-ARCH-POLICY-003", "architecture policy must be a regular non-symlink file")
    return safe


def load_project_architecture_policy(project_root: Path) -> ArchitecturePolicyLayer | None:
    path = _policy_file(project_root)
    if path is None:
        return None
    try:
        with path.open("rb") as stream:
            data = stream.read(ARCHITECTURE_POLICY_MAX_BYTES + 1)
    except OSError as exc:
        raise _fail("SDAI-ARCH-POLICY-003", "unable to read architecture policy") from exc
    if len(data) > ARCHITECTURE_POLICY_MAX_BYTES:
        raise _fail("SDAI-ARCH-POLICY-003", "architecture policy exceeds size limit")
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail("SDAI-ARCH-POLICY-003", "architecture policy must be valid UTF-8") from exc
    try:
        raw = yaml.load(text, Loader=UniqueKeySafeLoader)
        value = normalize_structured_json(raw, max_nodes=50_000, max_depth=32)
    except (yaml.YAMLError, ValueError) as exc:
        raise _fail("SDAI-ARCH-POLICY-002", "architecture policy must be bounded unique-key YAML") from exc
    if not isinstance(value, Mapping):
        raise _fail("SDAI-ARCH-POLICY-002", "architecture policy root must be a mapping")
    allowed = {"apiVersion", "kind", "blockSeverities", "blockCodes"}
    if set(value) - allowed:
        unknown = ", ".join(sorted(set(value) - allowed))
        raise _fail(
            "SDAI-ARCH-POLICY-004",
            f"architecture policy contains unsupported/weakening fields: {unknown}",
        )
    if value.get("apiVersion") != ARCHITECTURE_POLICY_API_VERSION or value.get("kind") != "ArchitecturePolicy":
        raise _fail("SDAI-ARCH-POLICY-002", "unsupported architecture policy API version/kind")
    severities_raw = value.get("blockSeverities", [])
    if not isinstance(severities_raw, list) or len(severities_raw) > len(ArchitectureDriftSeverity):
        raise _fail("SDAI-ARCH-POLICY-002", "blockSeverities must be a bounded list")
    severities = tuple(_severity(item) for item in severities_raw if isinstance(item, str))
    if len(severities) != len(severities_raw):
        raise _fail("SDAI-ARCH-POLICY-002", "blockSeverities must contain text values")
    return ArchitecturePolicyLayer(
        "project",
        block_severities=severities,
        block_codes=_bounded_codes(value.get("blockCodes"), label="blockCodes"),
    )


@dataclass(frozen=True, slots=True)
class ArchitectureCheckResult:
    feature_id: str
    report: ArchitectureDriftReport
    policy: EffectiveArchitecturePolicy
    blocking_codes: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.blocking_codes)

    @property
    def status(self) -> str:
        return "blocked" if self.blocked else "passed"

    @property
    def sha256(self) -> str:
        return _hash(self.to_dict(include_sha=False))

    def to_dict(self, *, include_sha: bool = True) -> dict[str, object]:
        report_value = json.loads(self.report.to_json())
        result: dict[str, object] = {
            "apiVersion": ARCHITECTURE_CHECK_API_VERSION,
            "kind": "ArchitectureCheck",
            "feature": self.feature_id,
            "status": self.status,
            "topologySha256": self.report.topology_sha256,
            "approvalTruthSha256": self.report.approval_truth_sha256,
            "reportSha256": self.report.sha256,
            "policy": self.policy.to_dict(),
            "policySha256": self.policy.sha256,
            "blockingCodes": list(self.blocking_codes),
            "report": report_value,
        }
        if include_sha:
            result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_json(self.to_dict()) + "\n"


def observe_architecture(
    project_root: Path,
    feature_id: str,
) -> tuple[ApprovedArchitecture, tuple[ArchitectureObservation, ...]]:
    root = project_root.resolve()
    approved = load_approved_architecture(root, feature_id)
    observers = (
        DependencyImportObserver(),
        ServiceCommunicationObserver(),
        RepositoryDataObserver(),
        DeploymentTopologyObserver(),
    )
    observations = tuple(observer.observe(root, approved) for observer in observers)
    return approved, observations


def check_architecture(
    project_root: Path,
    feature_id: str,
    *,
    organization_policy: ArchitecturePolicyLayer | None = None,
    project_policy: ArchitecturePolicyLayer | None = None,
) -> ArchitectureCheckResult:
    root = project_root.resolve()
    approved, observations = observe_architecture(root, feature_id)
    report = evaluate_trust_boundary_security(approved, observations)
    policy = resolve_architecture_policy(
        organization_policy,
        project_policy if project_policy is not None else load_project_architecture_policy(root),
    )
    blocking = tuple(sorted({finding.code for finding in report.findings if policy.blocks(finding)}))
    return ArchitectureCheckResult(feature_id, report, policy, blocking)


__all__ = [
    "ARCHITECTURE_BLOCKED_EXIT_CODE",
    "ARCHITECTURE_CHECK_API_VERSION",
    "ARCHITECTURE_POLICY_API_VERSION",
    "ARCHITECTURE_POLICY_PATH",
    "ArchitectureCheckResult",
    "ArchitectureEngineError",
    "ArchitecturePolicyLayer",
    "CORE_ARCHITECTURE_POLICY",
    "EffectiveArchitecturePolicy",
    "check_architecture",
    "load_project_architecture_policy",
    "observe_architecture",
    "resolve_architecture_policy",
]
