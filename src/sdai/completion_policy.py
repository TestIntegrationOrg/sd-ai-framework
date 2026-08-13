from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import re
from typing import Mapping, Sequence

import yaml

from sdai.path_safety import PathSafetyError, ensure_within_project


COMPLETION_POLICY_API_VERSION = "sdai.completion-policy/v1"
SPEC_REVIEW_CONTRACT = "sdai.completion/spec-review/v1"
CODE_QUALITY_REVIEW_CONTRACT = "sdai.completion/code-quality-review/v1"
FINAL_REVIEW_CONTRACT = "sdai.completion/final-review/v1"
TEST_CONTRACT = "sdai.completion/test/v1"
QUALITY_CONTRACT = "sdai.completion/quality/v1"
SECURITY_CONTRACT = "sdai.completion/security/v1"
APPROVAL_CONTRACT = "sdai.completion/approval/v1"
VERIFICATION_CONTRACT = "sdai.completion/verification/v1"

_CONTRACT = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,63}(?:/[a-z0-9][a-z0-9._-]{0,63})+$"
)
_POLICY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class CompletionPolicyError(RuntimeError):
    """Raised when completion evidence policy cannot be resolved safely."""


class CompletionRisk(str, Enum):
    TRIVIAL = "trivial"
    STANDARD = "standard"
    CRITICAL = "critical"
    REGULATED = "regulated"


class CompletionScope(str, Enum):
    TASK = "task"
    CHANGE = "change"


class CompletionPolicyLayer(str, Enum):
    BUILTIN = "builtin"
    ORG = "org"
    REPO = "repo"
    USER = "user"

    @property
    def priority(self) -> int:
        return {
            CompletionPolicyLayer.BUILTIN: 0,
            CompletionPolicyLayer.ORG: 10,
            CompletionPolicyLayer.REPO: 20,
            CompletionPolicyLayer.USER: 30,
        }[self]


_DEFAULTS: Mapping[
    CompletionRisk,
    Mapping[CompletionScope, tuple[str, ...]],
] = {
    CompletionRisk.TRIVIAL: {
        CompletionScope.TASK: (),
        CompletionScope.CHANGE: (),
    },
    CompletionRisk.STANDARD: {
        CompletionScope.TASK: (
            SPEC_REVIEW_CONTRACT,
            CODE_QUALITY_REVIEW_CONTRACT,
            TEST_CONTRACT,
        ),
        CompletionScope.CHANGE: (
            FINAL_REVIEW_CONTRACT,
            VERIFICATION_CONTRACT,
        ),
    },
    CompletionRisk.CRITICAL: {
        CompletionScope.TASK: (
            SPEC_REVIEW_CONTRACT,
            CODE_QUALITY_REVIEW_CONTRACT,
            TEST_CONTRACT,
            QUALITY_CONTRACT,
            SECURITY_CONTRACT,
            VERIFICATION_CONTRACT,
        ),
        CompletionScope.CHANGE: (
            FINAL_REVIEW_CONTRACT,
            QUALITY_CONTRACT,
            SECURITY_CONTRACT,
            VERIFICATION_CONTRACT,
        ),
    },
    CompletionRisk.REGULATED: {
        CompletionScope.TASK: (
            SPEC_REVIEW_CONTRACT,
            CODE_QUALITY_REVIEW_CONTRACT,
            TEST_CONTRACT,
            QUALITY_CONTRACT,
            SECURITY_CONTRACT,
            APPROVAL_CONTRACT,
            VERIFICATION_CONTRACT,
        ),
        CompletionScope.CHANGE: (
            FINAL_REVIEW_CONTRACT,
            QUALITY_CONTRACT,
            SECURITY_CONTRACT,
            APPROVAL_CONTRACT,
            VERIFICATION_CONTRACT,
        ),
    },
}


def _fail(code: str, message: str) -> CompletionPolicyError:
    return CompletionPolicyError(f"{code}: {message}")


def validate_completion_contract(
    value: object,
    *,
    label: str = "completion evidence contract",
) -> str:
    if not isinstance(value, str) or not _CONTRACT.fullmatch(value.strip()):
        raise _fail("SDAI-COMPLETION-POLICY-001", f"{label} is invalid")
    return value.strip()


def _risk(value: CompletionRisk | str) -> CompletionRisk:
    try:
        return (
            value
            if isinstance(value, CompletionRisk)
            else CompletionRisk(str(value).strip().lower())
        )
    except ValueError as exc:
        raise _fail(
            "SDAI-COMPLETION-POLICY-001",
            "unsupported completion risk",
        ) from exc


def _scope(value: CompletionScope | str) -> CompletionScope:
    try:
        return (
            value
            if isinstance(value, CompletionScope)
            else CompletionScope(str(value).strip().lower())
        )
    except ValueError as exc:
        raise _fail(
            "SDAI-COMPLETION-POLICY-001",
            "unsupported completion scope",
        ) from exc


@dataclass(frozen=True)
class CompletionPolicyContribution:
    layer: CompletionPolicyLayer
    source: str
    policy_id: str
    contracts: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer.value,
            "source": self.source,
            "policy_id": self.policy_id,
            "contracts": list(self.contracts),
        }


@dataclass(frozen=True)
class CompletionPolicyResolution:
    risk: CompletionRisk
    scope: CompletionScope
    required_contracts: tuple[str, ...]
    contributions: tuple[CompletionPolicyContribution, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "risk": self.risk.value,
            "scope": self.scope.value,
            "required_contracts": list(self.required_contracts),
            "contributions": [item.as_dict() for item in self.contributions],
        }


def builtin_completion_policy(
    risk: CompletionRisk | str,
    scope: CompletionScope | str,
    *,
    additional_required: Sequence[str] = (),
) -> CompletionPolicyResolution:
    selected_risk = _risk(risk)
    selected_scope = _scope(scope)
    builtin = tuple(sorted(_DEFAULTS[selected_risk][selected_scope]))
    contributions = [
        CompletionPolicyContribution(
            CompletionPolicyLayer.BUILTIN,
            "builtin",
            "sdai-default",
            builtin,
        )
    ]
    extra = tuple(
        sorted(
            validate_completion_contract(
                item,
                label="additional required evidence",
            )
            for item in additional_required
        )
    )
    if len(extra) != len(set(extra)):
        raise _fail(
            "SDAI-COMPLETION-POLICY-001",
            "additional required evidence contains duplicates",
        )
    if extra:
        contributions.append(
            CompletionPolicyContribution(
                CompletionPolicyLayer.USER,
                "caller",
                "additional-required",
                extra,
            )
        )
    required = tuple(
        sorted(
            {
                contract
                for contribution in contributions
                for contract in contribution.contracts
            }
        )
    )
    return CompletionPolicyResolution(
        selected_risk,
        selected_scope,
        required,
        tuple(contributions),
    )


__all__ = [
    "APPROVAL_CONTRACT",
    "CODE_QUALITY_REVIEW_CONTRACT",
    "COMPLETION_POLICY_API_VERSION",
    "CompletionPolicyContribution",
    "CompletionPolicyError",
    "CompletionPolicyLayer",
    "CompletionPolicyResolution",
    "CompletionRisk",
    "CompletionScope",
    "FINAL_REVIEW_CONTRACT",
    "QUALITY_CONTRACT",
    "SECURITY_CONTRACT",
    "SPEC_REVIEW_CONTRACT",
    "TEST_CONTRACT",
    "VERIFICATION_CONTRACT",
    "builtin_completion_policy",
    "validate_completion_contract",
]
