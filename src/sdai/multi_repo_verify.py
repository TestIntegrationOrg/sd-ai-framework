from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from sdai.multi_repo_run import (
    MultiRepoExitClass,
    MultiRepoRunPlan,
    RunParticipantStatus,
    build_multi_repo_run_plan,
)
from sdai.verification import VerificationRisk, verify_feature


@dataclass(frozen=True)
class RepositoryVerificationResult:
    repository_id: str
    status: str
    exit_code: int
    report_json: str | None = None
    reason: str | None = None

    @property
    def report(self) -> dict[str, object] | None:
        if self.report_json is None:
            return None
        value = json.loads(self.report_json)
        if not isinstance(value, dict):
            raise ValueError("repository verification report must be a mapping")
        return value

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "exitCode": self.exit_code,
            "repositoryId": self.repository_id,
            "status": self.status,
        }
        if self.report is not None:
            payload["report"] = self.report
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True)
class MultiRepoVerificationReport:
    feature_id: str
    graph_sha256: str
    plan_sha256: str
    repositories: tuple[RepositoryVerificationResult, ...]
    exit_class: MultiRepoExitClass

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": "sdai.multi-repo-verification/v1",
            "exitClass": self.exit_class.name.lower().replace("_", "-"),
            "featureId": self.feature_id,
            "graphSha256": self.graph_sha256,
            "planSha256": self.plan_sha256,
            "repositories": [item.as_dict() for item in self.repositories],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )


def verify_all_repositories(
    project_root: Path,
    feature_id: str,
    *,
    risk: VerificationRisk = VerificationRisk.MEDIUM,
) -> MultiRepoVerificationReport:
    plan = build_multi_repo_run_plan(
        project_root,
        feature_id,
        workflow="verification",
        isolation="in-place",
    )
    results: list[RepositoryVerificationResult] = []

    if plan.blockers:
        return MultiRepoVerificationReport(
            plan.feature_id,
            plan.graph_sha256,
            plan.sha256,
            (),
            plan.exit_class,
        )

    participant_problem = False
    policy_failure = False
    infrastructure_failure = False
    for participant in plan.participants:
        if participant.status is not RunParticipantStatus.READY or participant.root is None:
            participant_problem = True
            results.append(
                RepositoryVerificationResult(
                    participant.repository_id,
                    participant.status.value,
                    int(MultiRepoExitClass.PARTICIPANT_UNAVAILABLE_OR_DIRTY),
                    reason=participant.reason,
                )
            )
            continue
        try:
            report = verify_feature(participant.root, plan.feature_id, risk=risk)
        except (OSError, RuntimeError, ValueError) as exc:
            infrastructure_failure = True
            results.append(
                RepositoryVerificationResult(
                    participant.repository_id,
                    "infrastructure-failed",
                    int(MultiRepoExitClass.INFRASTRUCTURE_OR_TOOL_FAILURE),
                    reason="repository verification could not be completed",
                )
            )
            continue
        rendered = report.to_json().strip()
        if report.exit_code != 0:
            policy_failure = True
        results.append(
            RepositoryVerificationResult(
                participant.repository_id,
                report.status.value,
                report.exit_code,
                rendered,
            )
        )

    if infrastructure_failure:
        exit_class = MultiRepoExitClass.INFRASTRUCTURE_OR_TOOL_FAILURE
    elif participant_problem:
        exit_class = MultiRepoExitClass.PARTICIPANT_UNAVAILABLE_OR_DIRTY
    elif policy_failure:
        exit_class = MultiRepoExitClass.POLICY_FAILURE
    else:
        exit_class = MultiRepoExitClass.SUCCESS
    return MultiRepoVerificationReport(
        plan.feature_id,
        plan.graph_sha256,
        plan.sha256,
        tuple(results),
        exit_class,
    )


__all__ = [
    "MultiRepoVerificationReport",
    "RepositoryVerificationResult",
    "verify_all_repositories",
]
