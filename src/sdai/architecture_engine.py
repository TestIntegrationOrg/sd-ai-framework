from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

from sdai.architecture_communication_observer import ServiceCommunicationObserver
from sdai.architecture_data_observer import RepositoryDataObserver
from sdai.architecture_dependency_observer import DependencyImportObserver
from sdai.architecture_deployment_observer import DeploymentTopologyObserver
from sdai.architecture_drift import (
    ArchitectureDriftError,
    ArchitectureDriftReport,
    ArchitectureObserverRegistry,
    architecture_topology_path,
    load_approved_architecture,
)
from sdai.architecture_policy import (
    ArchitecturePolicyDecision,
    EffectiveArchitecturePolicy,
    evaluate_architecture_policy,
    load_effective_architecture_policy,
)
from sdai.architecture_security_drift import evaluate_trust_boundary_security
from sdai.models import validate_feature_id


ARCHITECTURE_EVALUATION_API_VERSION = "sdai.architecture-drift-evaluation/v1"


@dataclass(frozen=True, slots=True)
class ArchitectureDriftEvaluation:
    feature_id: str
    topology_present: bool
    policy: EffectiveArchitecturePolicy
    decision: ArchitecturePolicyDecision
    report: ArchitectureDriftReport | None
    governance_error: str | None
    sha256: str

    @property
    def blocked(self) -> bool:
        return self.decision.blocked

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "apiVersion": ARCHITECTURE_EVALUATION_API_VERSION,
            "kind": "ArchitectureDriftEvaluation",
            "featureId": self.feature_id,
            "topologyPresent": self.topology_present,
            "policy": self.policy.to_dict(),
            "decision": self.decision.to_dict(),
            "report": self.report.to_dict() if self.report is not None else None,
            "governanceError": self.governance_error,
        }

    def to_dict(self) -> dict[str, object]:
        value = self._unsigned_dict()
        value["sha256"] = self.sha256
        return value

    def to_json(self) -> str:
        return _canonical_json(self.to_dict()) + "\n"


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
        raise ArchitectureDriftError(
            f"SDAI-ARCH-ENGINE-001: architecture evaluation is not canonical JSON: {exc}"
        ) from exc


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _evaluation(
    *,
    feature_id: str,
    topology_present: bool,
    policy: EffectiveArchitecturePolicy,
    decision: ArchitecturePolicyDecision,
    report: ArchitectureDriftReport | None,
    governance_error: str | None,
) -> ArchitectureDriftEvaluation:
    unsigned = {
        "apiVersion": ARCHITECTURE_EVALUATION_API_VERSION,
        "kind": "ArchitectureDriftEvaluation",
        "featureId": feature_id,
        "topologyPresent": topology_present,
        "policy": policy.to_dict(),
        "decision": decision.to_dict(),
        "report": report.to_dict() if report is not None else None,
        "governanceError": governance_error,
    }
    return ArchitectureDriftEvaluation(
        feature_id=feature_id,
        topology_present=topology_present,
        policy=policy,
        decision=decision,
        report=report,
        governance_error=governance_error,
        sha256=_hash_json(unsigned),
    )


def default_architecture_observer_registry() -> ArchitectureObserverRegistry:
    """Return the provider-independent 0.17 repository observer set."""
    return ArchitectureObserverRegistry(
        (
            DependencyImportObserver(),
            ServiceCommunicationObserver(),
            RepositoryDataObserver(),
            DeploymentTopologyObserver(),
        )
    )


def evaluate_architecture_drift(
    project_root: Path,
    feature_id: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> ArchitectureDriftEvaluation:
    """Evaluate approved architecture, repository reality, and monotonic governance.

    No provider, build, deployment tool, database, or network endpoint is invoked.
    Invalid/missing architecture approval is a governance blocker rather than an
    authority bypass. Malformed/unsafe topology or observer inputs still fail closed
    with ``ArchitectureDriftError`` so CLI callers can use exit code 1.
    """
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    policy = load_effective_architecture_policy(root, environ=environ)
    topology_path = architecture_topology_path(root, feature)
    if topology_path.is_symlink():
        raise ArchitectureDriftError(
            "SDAI-ARCH-ENGINE-002: approved architecture topology must not be a symbolic link"
        )
    topology_present = topology_path.exists()
    if not topology_present:
        decision = evaluate_architecture_policy(
            feature,
            policy,
            None,
            topology_present=False,
        )
        return _evaluation(
            feature_id=feature,
            topology_present=False,
            policy=policy,
            decision=decision,
            report=None,
            governance_error=None,
        )

    try:
        approved = load_approved_architecture(root, feature)
    except ArchitectureDriftError as exc:
        if not str(exc).startswith("SDAI-ARCH-DRIFT-005:"):
            raise
        governance_error = str(exc)
        decision = evaluate_architecture_policy(
            feature,
            policy,
            None,
            topology_present=True,
            governance_error=governance_error,
        )
        return _evaluation(
            feature_id=feature,
            topology_present=True,
            policy=policy,
            decision=decision,
            report=None,
            governance_error=governance_error,
        )

    observations = default_architecture_observer_registry().observe_all(root, approved)
    report = evaluate_trust_boundary_security(approved, observations)
    decision = evaluate_architecture_policy(
        feature,
        policy,
        report,
        topology_present=True,
    )
    return _evaluation(
        feature_id=feature,
        topology_present=True,
        policy=policy,
        decision=decision,
        report=report,
        governance_error=None,
    )


__all__ = [
    "ARCHITECTURE_EVALUATION_API_VERSION",
    "ArchitectureDriftEvaluation",
    "default_architecture_observer_registry",
    "evaluate_architecture_drift",
]
