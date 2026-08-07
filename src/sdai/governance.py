from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from sdai.artifacts import write_text
from sdai.config import load_yaml
from sdai.models import FeatureContext, LifecycleMode


class GovernanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalPolicy:
    gate: str
    min_approvals: int = 1
    required_roles: tuple[str, ...] = ()
    allowed_approvers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovalDecision:
    gate: str
    satisfied: bool
    approvals: int
    required: int
    missing_roles: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class GovernanceFinding:
    level: str
    code: str
    message: str


def load_approval_policies(project_root: Path) -> dict[str, ApprovalPolicy]:
    path = project_root / ".sdai" / "approval-policies.yaml"
    if not path.exists():
        return {}
    data = load_yaml(path)
    raw_gates = data.get("gates") or {}
    if not isinstance(raw_gates, dict):
        raise GovernanceError("approval-policies.yaml 'gates' must be a mapping")
    result: dict[str, ApprovalPolicy] = {}
    for gate, raw in raw_gates.items():
        if not isinstance(raw, dict):
            raise GovernanceError(f"Approval policy '{gate}' must be a mapping")
        required_roles = raw.get("required_roles") or []
        allowed = raw.get("allowed_approvers") or []
        if not isinstance(required_roles, list) or not all(isinstance(v, str) for v in required_roles):
            raise GovernanceError(f"Approval policy '{gate}' required_roles must be a string list")
        if not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed):
            raise GovernanceError(f"Approval policy '{gate}' allowed_approvers must be a string list")
        minimum = int(raw.get("min_approvals", 1))
        if minimum < 1:
            raise GovernanceError(f"Approval policy '{gate}' min_approvals must be >= 1")
        result[str(gate)] = ApprovalPolicy(
            gate=str(gate),
            min_approvals=minimum,
            required_roles=tuple(value.strip() for value in required_roles if value.strip()),
            allowed_approvers=tuple(value.strip() for value in allowed if value.strip()),
        )
    return result


def _approval_path(context: FeatureContext, gate: str) -> Path:
    return context.artifact(f"approvals/{gate}.yaml")


def _load_approval_document(context: FeatureContext, gate: str) -> dict[str, Any]:
    path = _approval_path(context, gate)
    if not path.exists():
        return {"version": 2, "gate": gate, "approvals": []}
    data = load_yaml(path)
    # Upgrade the v0.3 one-record format in memory.
    if "approvals" not in data and data.get("approved_by"):
        data["approvals"] = [
            {
                "approved_by": data.get("approved_by"),
                "approved_at": data.get("approved_at"),
                "role": data.get("role") or "",
                "note": data.get("note") or "",
            }
        ]
    return data


def record_approval(
    context: FeatureContext,
    gate: str,
    *,
    approved_by: str,
    role: str = "",
    note: str = "",
) -> ApprovalDecision:
    approved_by = approved_by.strip()
    role = role.strip()
    if not approved_by:
        raise GovernanceError("approved_by is required")

    policy = load_approval_policies(context.project_root).get(gate, ApprovalPolicy(gate=gate))
    if policy.allowed_approvers and approved_by not in policy.allowed_approvers:
        raise GovernanceError(f"Approver '{approved_by}' is not allowed for gate '{gate}'")
    if policy.required_roles and role not in policy.required_roles:
        required = ", ".join(policy.required_roles)
        raise GovernanceError(f"Gate '{gate}' requires one of these roles: {required}")

    document = _load_approval_document(context, gate)
    approvals = document.get("approvals") or []
    if not isinstance(approvals, list):
        raise GovernanceError(f"Approval artifact for '{gate}' has invalid approvals data")

    # Re-approval by the same identity replaces the previous record so distinct
    # approvers, not repeated clicks, satisfy min_approvals.
    approvals = [item for item in approvals if str(item.get("approved_by") or "") != approved_by]
    approvals.append(
        {
            "approved_by": approved_by,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "note": note.strip(),
        }
    )
    document = {
        "version": 2,
        "gate": gate,
        "approvals": approvals,
    }
    decision = evaluate_approval(context, gate, document=document)
    document["status"] = "approved" if decision.satisfied else "pending"
    write_text(_approval_path(context, gate), yaml.safe_dump(document, sort_keys=False))
    return decision


def evaluate_approval(
    context: FeatureContext,
    gate: str,
    *,
    document: dict[str, Any] | None = None,
) -> ApprovalDecision:
    policy = load_approval_policies(context.project_root).get(gate, ApprovalPolicy(gate=gate))
    document = document if document is not None else _load_approval_document(context, gate)
    approvals = document.get("approvals") or []
    if not isinstance(approvals, list):
        return ApprovalDecision(gate, False, 0, policy.min_approvals, detail="invalid approval artifact")

    identities = {str(item.get("approved_by") or "") for item in approvals if item.get("approved_by")}
    roles = {str(item.get("role") or "") for item in approvals if item.get("role")}
    missing_roles = tuple(role for role in policy.required_roles if role not in roles)
    satisfied = len(identities) >= policy.min_approvals and not missing_roles
    detail = (
        f"{len(identities)}/{policy.min_approvals} distinct approvals; "
        f"missing roles={','.join(missing_roles) or '-'}"
    )
    return ApprovalDecision(
        gate=gate,
        satisfied=satisfied,
        approvals=len(identities),
        required=policy.min_approvals,
        missing_roles=missing_roles,
        detail=detail,
    )


def scaffold_approval_policies() -> str:
    data = {
        "version": 1,
        "gates": {
            "architecture": {
                "min_approvals": 1,
                "required_roles": ["architect"],
                "allowed_approvers": [],
            },
            "security": {
                "min_approvals": 1,
                "required_roles": ["security"],
                "allowed_approvers": [],
            },
        },
    }
    return yaml.safe_dump(data, sort_keys=False)


def scaffold_governance() -> str:
    data = {
        "version": 1,
        "workflow": {
            # Existing projects remain compatible until an organization explicitly
            # turns policy enforcement on.
            "enforce": False,
            "max_parallelism": 4,
            "allowed_workspace_write_profiles": ["codex", "copilot", "claude", "gemini"],
            "require_prior_approval_for_workspace_write": True,
        },
        "quality": {
            "required_gates": {
                "light": [],
                "standard": [],
                "critical": [],
            }
        },
    }
    return yaml.safe_dump(data, sort_keys=False)


def load_governance(project_root: Path) -> dict[str, Any]:
    path = project_root / ".sdai" / "governance.yaml"
    return load_yaml(path) if path.exists() else {}


def governance_enforced(project_root: Path) -> bool:
    data = load_governance(project_root)
    return bool((data.get("workflow") or {}).get("enforce", False))


def check_workflow_governance(project_root: Path, definition: Any) -> list[GovernanceFinding]:
    """Static policy checks for a loaded workflow definition.

    Kept duck-typed to avoid coupling policy parsing back into the workflow module.
    """
    policy = load_governance(project_root)
    workflow_policy = policy.get("workflow") or {}
    quality_policy = policy.get("quality") or {}
    findings: list[GovernanceFinding] = []

    allowed_profiles = set(workflow_policy.get("allowed_workspace_write_profiles") or [])
    max_parallelism = int(workflow_policy.get("max_parallelism", 4))
    seen_quality_gates: set[str] = set()

    for step in definition.steps:
        kind = str(step.kind.value)
        if kind == "agent" and str(step.mode.value) == "workspace-write":
            if step.profile and allowed_profiles and step.profile not in allowed_profiles:
                findings.append(
                    GovernanceFinding(
                        "ERROR",
                        "PROFILE_NOT_ALLOWED",
                        f"Step '{step.id}' uses workspace-write profile '{step.profile}' which policy does not allow",
                    )
                )
        if kind == "parallel":
            children = getattr(step, "children", ()) or ()
            if len(children) > max_parallelism:
                findings.append(
                    GovernanceFinding(
                        "ERROR",
                        "PARALLELISM_LIMIT",
                        f"Step '{step.id}' has {len(children)} children; policy max_parallelism is {max_parallelism}",
                    )
                )
        if kind == "quality-gate" and getattr(step, "quality_gate", None):
            seen_quality_gates.add(step.quality_gate)

    mode = getattr(definition, "validation_mode", LifecycleMode.STANDARD)
    required = set((quality_policy.get("required_gates") or {}).get(mode.value, []) or [])
    for missing in sorted(required - seen_quality_gates):
        findings.append(
            GovernanceFinding(
                "ERROR",
                "QUALITY_GATE_REQUIRED",
                f"Workflow '{definition.name}' requires quality gate '{missing}' for {mode.value} validation",
            )
        )
    return findings
