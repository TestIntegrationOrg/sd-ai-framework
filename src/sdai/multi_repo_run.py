from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Callable, Iterable

from sdai.feature_repositories import (
    FeatureRepositoryError,
    ResolvedFeatureRepository,
    resolve_feature_repositories,
)
from sdai.models import validate_feature_id
from sdai.multi_repo_feature_graph import (
    FeatureGraphFindingLevel,
    MultiRepoFeatureGraph,
    build_multi_repo_feature_graph,
)
from sdai.worktree_isolation import (
    GitBaselineEvidence,
    WorktreeIsolationError,
    verify_clean_baseline,
)


MULTI_REPO_RUN_PLAN_API_VERSION = "sdai.multi-repo-run-plan/v1"


class MultiRepoRunError(RuntimeError):
    """Raised when a multi-repository execution cannot be planned safely."""


class MultiRepoExitClass(IntEnum):
    SUCCESS = 0
    POLICY_FAILURE = 2
    DRIFT = 4
    PARTICIPANT_UNAVAILABLE_OR_DIRTY = 5
    INFRASTRUCTURE_OR_TOOL_FAILURE = 6


class RunParticipantStatus(StrEnum):
    READY = "ready"
    DIRTY = "dirty"
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _fail(code: str, message: str) -> MultiRepoRunError:
    return MultiRepoRunError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-MULTI-RUN-001", "run plan must be canonical finite JSON") from exc


def _sha256_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_feature(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-_")
    if not candidate:
        raise _fail("SDAI-MULTI-RUN-001", "feature id cannot form a portable branch name")
    return candidate[:80]


def _baseline_payload(baseline: GitBaselineEvidence) -> dict[str, object]:
    return {
        "branch": baseline.branch,
        "clean": baseline.clean,
        "commit": baseline.commit,
        "repositoryIdentity": baseline.repository_identity,
        "statusSha256": baseline.status_sha256,
        "tree": baseline.tree,
    }


def _participant_ids(graph: MultiRepoFeatureGraph) -> tuple[str, ...]:
    values: set[str] = set()
    for edge in graph.edges:
        if edge.relation == "owned-by" and edge.target.startswith("repository:"):
            values.add(edge.target.removeprefix("repository:"))
    for node in graph.nodes:
        if node.type.value != "repository" or not node.node_id.startswith("repository:"):
            continue
        if any(fact.kind == "repository-trace" for fact in node.facts):
            values.add(node.node_id.removeprefix("repository:"))
    return tuple(sorted(values))


def _blocking_graph_findings(graph: MultiRepoFeatureGraph) -> tuple[str, ...]:
    blocking_codes = {
        "SDAI-FEATURE-GRAPH-STALE-CONTENT",
        "SDAI-FEATURE-GRAPH-MISSING-REPOSITORIES",
        "SDAI-FEATURE-GRAPH-MISSING-REPOSITORY",
        "SDAI-FEATURE-GRAPH-AMBIGUOUS-REPOSITORIES",
        "SDAI-FEATURE-GRAPH-AMBIGUOUS-ROUTING",
        "SDAI-FEATURE-GRAPH-MISSING-OWNERSHIP",
        "SDAI-FEATURE-GRAPH-AMBIGUOUS-TRACE",
    }
    return tuple(
        sorted(
            {
                finding.code
                for finding in graph.findings
                if finding.level is FeatureGraphFindingLevel.ERROR
                and finding.code in blocking_codes
            }
        )
    )


@dataclass(frozen=True)
class PlannedRepositoryRun:
    repository_id: str
    root: Path | None
    required: bool
    ordinal: int
    status: RunParticipantStatus
    baseline_json: str | None
    command: str
    branch_policy: str
    reason: str | None = None

    def __post_init__(self) -> None:
        try:
            status = self.status if isinstance(self.status, RunParticipantStatus) else RunParticipantStatus(self.status)
        except ValueError as exc:
            raise _fail("SDAI-MULTI-RUN-001", f"unsupported participant status: {self.status!r}") from exc
        object.__setattr__(self, "status", status)
        if self.baseline_json is not None:
            parsed = json.loads(self.baseline_json)
            canonical = _canonical_json(parsed)
            if canonical != self.baseline_json:
                raise _fail("SDAI-MULTI-RUN-001", "baseline_json must be canonical JSON")

    @property
    def baseline(self) -> dict[str, object] | None:
        if self.baseline_json is None:
            return None
        value = json.loads(self.baseline_json)
        if not isinstance(value, dict):
            raise _fail("SDAI-MULTI-RUN-001", "baseline must be a mapping")
        return value

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "branchPolicy": self.branch_policy,
            "command": self.command,
            "ordinal": self.ordinal,
            "repositoryId": self.repository_id,
            "required": self.required,
            "status": self.status.value,
        }
        if self.baseline is not None:
            payload["baseline"] = self.baseline
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True)
class MultiRepoRunPlan:
    feature_id: str
    graph_sha256: str
    repository_resolution_sha256: str | None
    store_resolution_sha256: str | None
    workflow: str
    isolation: str
    participants: tuple[PlannedRepositoryRun, ...]
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", validate_feature_id(self.feature_id))
        for label, value in (
            ("graph_sha256", self.graph_sha256),
            ("repository_resolution_sha256", self.repository_resolution_sha256),
            ("store_resolution_sha256", self.store_resolution_sha256),
        ):
            if value is not None and (not isinstance(value, str) or not _SHA256.fullmatch(value)):
                raise _fail("SDAI-MULTI-RUN-001", f"{label} must be a SHA-256 digest")
        if self.isolation not in {"in-place", "worktree"}:
            raise _fail("SDAI-MULTI-RUN-001", "isolation must be in-place or worktree")
        ordered = tuple(sorted(self.participants, key=lambda item: (item.ordinal, item.repository_id)))
        ids = [item.repository_id for item in ordered]
        if len(ids) != len(set(ids)):
            raise _fail("SDAI-MULTI-RUN-001", "run plan contains duplicate repository ids")
        object.__setattr__(self, "participants", ordered)
        object.__setattr__(self, "blockers", tuple(sorted(set(self.blockers))))

    @property
    def ready(self) -> bool:
        return not self.blockers and all(item.status is RunParticipantStatus.READY for item in self.participants)

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": MULTI_REPO_RUN_PLAN_API_VERSION,
            "blockers": list(self.blockers),
            "featureId": self.feature_id,
            "graphSha256": self.graph_sha256,
            "isolation": self.isolation,
            "participants": [item.as_dict() for item in self.participants],
            "repositoryResolutionSha256": self.repository_resolution_sha256,
            "storeResolutionSha256": self.store_resolution_sha256,
            "workflow": self.workflow,
        }

    @property
    def sha256(self) -> str:
        return _sha256_json(self.as_dict())

    def to_json(self) -> str:
        payload = self.as_dict()
        payload["planSha256"] = self.sha256
        return _canonical_json(payload)

    @property
    def exit_class(self) -> MultiRepoExitClass:
        if self.blockers:
            if "SDAI-FEATURE-GRAPH-MISSING-REPOSITORY" in self.blockers:
                return MultiRepoExitClass.PARTICIPANT_UNAVAILABLE_OR_DIRTY
            return MultiRepoExitClass.DRIFT
        if any(
            item.status
            in {
                RunParticipantStatus.DIRTY,
                RunParticipantStatus.UNAVAILABLE,
                RunParticipantStatus.INCOMPATIBLE,
            }
            for item in self.participants
        ):
            return MultiRepoExitClass.PARTICIPANT_UNAVAILABLE_OR_DIRTY
        return MultiRepoExitClass.SUCCESS


def _inspect_resolved_repository(
    feature_id: str,
    workflow: str,
    isolation: str,
    repository: ResolvedFeatureRepository,
) -> PlannedRepositoryRun:
    item = repository.repository
    command = f"sdai run {feature_id} --repo {item.id} --workflow {workflow} --isolation {isolation}"
    branch_policy = (
        "in-place-current-branch"
        if isolation == "in-place"
        else f"sdai/{_safe_feature(feature_id)}/<runtime-run-id>"
    )
    if repository.root is None:
        return PlannedRepositoryRun(
            item.id,
            None,
            item.required,
            repository.ordinal,
            RunParticipantStatus.UNAVAILABLE,
            None,
            command,
            branch_policy,
            "declared repository is unavailable",
        )
    if not (repository.root / ".sdai" / "config.yaml").is_file():
        return PlannedRepositoryRun(
            item.id,
            repository.root,
            item.required,
            repository.ordinal,
            RunParticipantStatus.INCOMPATIBLE,
            None,
            command,
            branch_policy,
            "repository is not initialized as an SD-AI project",
        )
    try:
        baseline = verify_clean_baseline(repository.root)
    except WorktreeIsolationError as exc:
        message = str(exc)
        status = RunParticipantStatus.DIRTY if "source baseline is dirty" in message else RunParticipantStatus.INCOMPATIBLE
        return PlannedRepositoryRun(
            item.id,
            repository.root,
            item.required,
            repository.ordinal,
            status,
            None,
            command,
            branch_policy,
            "repository baseline is dirty" if status is RunParticipantStatus.DIRTY else "repository baseline is incompatible",
        )
    return PlannedRepositoryRun(
        item.id,
        repository.root,
        item.required,
        repository.ordinal,
        RunParticipantStatus.READY,
        _canonical_json(_baseline_payload(baseline)),
        command,
        branch_policy,
    )


def build_multi_repo_run_plan(
    project_root: Path,
    feature_id: str,
    *,
    repository_ids: Iterable[str] | None = None,
    workflow: str = "standard",
    isolation: str = "worktree",
) -> MultiRepoRunPlan:
    root = Path(project_root).resolve()
    feature = validate_feature_id(feature_id)
    graph = build_multi_repo_feature_graph(root, feature)
    blockers = list(_blocking_graph_findings(graph))
    selected_requested = tuple(sorted(set(repository_ids or ())))

    try:
        resolved = resolve_feature_repositories(root)
    except FeatureRepositoryError:
        return MultiRepoRunPlan(
            feature,
            graph.sha256,
            graph.repository_resolution_sha256,
            graph.store_resolution_sha256,
            workflow,
            isolation,
            (),
            tuple(blockers or ["SDAI-FEATURE-GRAPH-MISSING-REPOSITORY"]),
        )

    participants = set(_participant_ids(graph))
    by_id = {item.repository.id: item for item in resolved.repositories}
    if selected_requested:
        unknown = sorted(set(selected_requested) - set(by_id))
        if unknown:
            raise _fail("SDAI-MULTI-RUN-002", "unknown repository id(s): " + ", ".join(unknown))
        nonparticipants = sorted(set(selected_requested) - participants)
        if nonparticipants:
            raise _fail(
                "SDAI-MULTI-RUN-002",
                "repository is not a participant in this feature: " + ", ".join(nonparticipants),
            )
        selected = selected_requested
    else:
        selected = tuple(sorted(participants))

    planned = tuple(
        _inspect_resolved_repository(feature, workflow, isolation, by_id[repository_id])
        for repository_id in selected
    )
    if not planned and not blockers:
        blockers.append("SDAI-MULTI-RUN-NO-PARTICIPANTS")
    return MultiRepoRunPlan(
        feature,
        graph.sha256,
        resolved.sha256,
        graph.store_resolution_sha256,
        workflow,
        isolation,
        planned,
        tuple(blockers),
    )


def revalidate_run_plan(plan: MultiRepoRunPlan) -> None:
    if plan.blockers:
        raise _fail("SDAI-MULTI-RUN-DRIFT", "run plan contains blocking feature-graph findings")
    for participant in plan.participants:
        if participant.status is not RunParticipantStatus.READY or participant.root is None:
            raise _fail(
                "SDAI-MULTI-RUN-PARTICIPANT",
                f"repository '{participant.repository_id}' is not ready",
            )
        expected = participant.baseline
        if expected is None:
            raise _fail("SDAI-MULTI-RUN-PARTICIPANT", "ready repository has no baseline binding")
        try:
            current = _baseline_payload(verify_clean_baseline(participant.root))
        except WorktreeIsolationError as exc:
            raise _fail(
                "SDAI-MULTI-RUN-PARTICIPANT",
                f"repository '{participant.repository_id}' baseline is no longer clean/compatible",
            ) from exc
        if current != expected:
            raise _fail(
                "SDAI-MULTI-RUN-DRIFT",
                f"repository '{participant.repository_id}' changed after the run plan was built",
            )


@dataclass(frozen=True)
class RepositoryRunResult:
    repository_id: str
    exit_code: int
    status: str

    def as_dict(self) -> dict[str, object]:
        return {"exitCode": self.exit_code, "repositoryId": self.repository_id, "status": self.status}


@dataclass(frozen=True)
class MultiRepoRunResult:
    plan_sha256: str
    results: tuple[RepositoryRunResult, ...]
    exit_class: MultiRepoExitClass

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": "sdai.multi-repo-run-result/v1",
            "exitClass": self.exit_class.name.lower().replace("_", "-"),
            "planSha256": self.plan_sha256,
            "repositories": [item.as_dict() for item in self.results],
        }

    def to_json(self) -> str:
        payload = self.as_dict()
        payload["sha256"] = _sha256_json(payload)
        return _canonical_json(payload)


def execute_multi_repo_run(
    plan: MultiRepoRunPlan,
    runner: Callable[[PlannedRepositoryRun, str, str], int],
) -> MultiRepoRunResult:
    if plan.exit_class is not MultiRepoExitClass.SUCCESS:
        return MultiRepoRunResult(plan.sha256, (), plan.exit_class)
    try:
        revalidate_run_plan(plan)
    except MultiRepoRunError as exc:
        exit_class = (
            MultiRepoExitClass.DRIFT
            if "SDAI-MULTI-RUN-DRIFT" in str(exc)
            else MultiRepoExitClass.PARTICIPANT_UNAVAILABLE_OR_DIRTY
        )
        return MultiRepoRunResult(plan.sha256, (), exit_class)

    results: list[RepositoryRunResult] = []
    exit_class = MultiRepoExitClass.SUCCESS
    for participant in plan.participants:
        try:
            code = int(runner(participant, plan.workflow, plan.isolation))
        except (OSError, RuntimeError):
            results.append(RepositoryRunResult(participant.repository_id, 1, "infrastructure-failed"))
            exit_class = MultiRepoExitClass.INFRASTRUCTURE_OR_TOOL_FAILURE
            break
        if code == 0:
            results.append(RepositoryRunResult(participant.repository_id, 0, "completed"))
            continue
        results.append(RepositoryRunResult(participant.repository_id, code, "policy-failed"))
        exit_class = MultiRepoExitClass.POLICY_FAILURE
        break
    return MultiRepoRunResult(plan.sha256, tuple(results), exit_class)


__all__ = [
    "MULTI_REPO_RUN_PLAN_API_VERSION",
    "MultiRepoExitClass",
    "MultiRepoRunError",
    "MultiRepoRunPlan",
    "MultiRepoRunResult",
    "PlannedRepositoryRun",
    "RepositoryRunResult",
    "RunParticipantStatus",
    "build_multi_repo_run_plan",
    "execute_multi_repo_run",
    "revalidate_run_plan",
]
