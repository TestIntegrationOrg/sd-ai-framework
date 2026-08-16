from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from sdai.multi_repo_pr_graph import build_multi_repo_feature_graph
from sdai.multi_repo_run import (
    MultiRepoExitClass,
    MultiRepoRunError,
    RunParticipantStatus,
    build_multi_repo_run_plan,
    revalidate_run_plan,
)
from sdai.verification import VerificationOutcome
from sdai.verify_engine import verify_feature


_RISKS = frozenset({"trivial", "standard", "critical", "regulated"})
_PR_FINDING_PREFIX = "SDAI-FEATURE-GRAPH-PR-EVIDENCE-"


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
    graph_findings_json: str
    repositories: tuple[RepositoryVerificationResult, ...]
    exit_class: MultiRepoExitClass

    @property
    def graph_findings(self) -> list[dict[str, object]]:
        value = json.loads(self.graph_findings_json)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError("graph findings must be a list of mappings")
        return value

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": "sdai.multi-repo-verification/v1",
            "exitClass": self.exit_class.name.lower().replace("_", "-"),
            "featureId": self.feature_id,
            "graphFindings": self.graph_findings,
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


def _normalize_risk(value: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if normalized not in _RISKS:
        raise ValueError("risk must be one of: " + ", ".join(sorted(_RISKS)))
    return normalized


def _graph_findings_json(project_root: Path, feature_id: str) -> tuple[str, str]:
    graph = build_multi_repo_feature_graph(project_root, feature_id)
    rendered = json.dumps(
        [item.as_dict() for item in graph.findings],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return graph.sha256, rendered


def _has_unsatisfied_pr_traceability(graph_findings_json: str) -> bool:
    value = json.loads(graph_findings_json)
    if not isinstance(value, list):
        return True
    for item in value:
        if not isinstance(item, dict):
            return True
        code = item.get("code")
        if isinstance(code, str) and code.startswith(_PR_FINDING_PREFIX):
            return True
    return False


def _revalidation_exit(plan) -> MultiRepoExitClass | None:
    if any(item.status is not RunParticipantStatus.READY for item in plan.participants):
        return None
    try:
        revalidate_run_plan(plan)
    except MultiRepoRunError as exc:
        if "SDAI-MULTI-RUN-DRIFT" in str(exc):
            return MultiRepoExitClass.DRIFT
        return MultiRepoExitClass.PARTICIPANT_UNAVAILABLE_OR_DIRTY
    return None


def verify_all_repositories(
    project_root: Path,
    feature_id: str,
    *,
    risk: str = "standard",
) -> MultiRepoVerificationReport:
    selected_risk = _normalize_risk(risk)
    graph_sha256, graph_findings_json = _graph_findings_json(project_root, feature_id)
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
            graph_sha256,
            plan.sha256,
            graph_findings_json,
            (),
            plan.exit_class,
        )

    revalidation_exit = _revalidation_exit(plan)
    if revalidation_exit is not None:
        return MultiRepoVerificationReport(
            plan.feature_id,
            graph_sha256,
            plan.sha256,
            graph_findings_json,
            (),
            revalidation_exit,
        )

    participant_problem = False
    policy_failure = _has_unsatisfied_pr_traceability(graph_findings_json)
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
            report = verify_feature(
                participant.root,
                plan.feature_id,
                risk=selected_risk,
                environ={},
            )
        except (OSError, RuntimeError, ValueError):
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
        rendered = report.to_json()
        if report.outcome is VerificationOutcome.PASSED:
            repository_exit = 0
        else:
            repository_exit = int(MultiRepoExitClass.POLICY_FAILURE)
            policy_failure = True
        results.append(
            RepositoryVerificationResult(
                participant.repository_id,
                report.outcome.value,
                repository_exit,
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
        graph_sha256,
        plan.sha256,
        graph_findings_json,
        tuple(results),
        exit_class,
    )


__all__ = [
    "MultiRepoVerificationReport",
    "RepositoryVerificationResult",
    "verify_all_repositories",
]
