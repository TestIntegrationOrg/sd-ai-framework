from __future__ import annotations

from pathlib import Path

from sdai.feature_repositories import FeatureRepositoryError, resolve_feature_repositories
from sdai.multi_repo_feature_graph import (
    FeatureGraphFact,
    FeatureGraphFinding,
    FeatureGraphFindingLevel,
    FeatureGraphNodeType,
    MultiRepoFeatureEdge,
    MultiRepoFeatureGraph,
    MultiRepoFeatureNode,
    build_multi_repo_feature_graph as build_base_multi_repo_feature_graph,
)
from sdai.pr_traceability import (
    PullRequestEvidenceError,
    PullRequestState,
    ResolvedPullRequestEvidence,
    resolve_pull_request_evidence,
)


_PR_ERROR_PREFIX = "SDAI-FEATURE-GRAPH-PR-EVIDENCE"


def _pr_node_id(repository_id: str, local_id: str) -> str:
    return f"pr-reference:{repository_id}:{local_id}"


def _finding(
    findings: list[FeatureGraphFinding],
    level: FeatureGraphFindingLevel,
    code: str,
    message: str,
    subject: str,
    repository_id: str,
) -> None:
    candidate = FeatureGraphFinding(level, code, message, subject, repository_id)
    if candidate not in findings:
        findings.append(candidate)


def _error_code(exc: PullRequestEvidenceError) -> str:
    message = str(exc)
    if "SDAI-PR-EVIDENCE-004" in message:
        return f"{_PR_ERROR_PREFIX}-CONFLICT"
    if "SDAI-PR-EVIDENCE-005" in message:
        return f"{_PR_ERROR_PREFIX}-CROSS-FEATURE"
    return f"{_PR_ERROR_PREFIX}-INVALID"


def _repository_trace_nodes(base: MultiRepoFeatureGraph) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for node in base.nodes:
        for fact in node.facts:
            if fact.kind != "trace-node":
                continue
            result.setdefault(fact.participant, set()).add(node.node_id)
    return result


def _participant_repositories(base: MultiRepoFeatureGraph) -> set[str]:
    participants: set[str] = set()
    for edge in base.edges:
        if edge.relation == "owned-by" and edge.target.startswith("repository:"):
            participants.add(edge.target.removeprefix("repository:"))
    for node in base.nodes:
        if node.type is not FeatureGraphNodeType.REPOSITORY:
            continue
        for fact in node.facts:
            if fact.kind == "repository-trace":
                participants.add(fact.participant)
    return participants


def _pr_fact(
    repository_id: str,
    evidence: ResolvedPullRequestEvidence,
    resolved_reference,
) -> FeatureGraphFact:
    reference = resolved_reference.reference
    payload: dict[str, object] = {
        "commitExists": resolved_reference.commit_exists,
        "commitReachable": resolved_reference.commit_reachable,
        "current": resolved_reference.current,
        "evidenceSha256": evidence.sha256,
        "headCommit": reference.head_commit,
        "links": list(reference.links),
        "localId": reference.id,
        "manifestSha256": evidence.manifest.sha256,
        "repositoryId": repository_id,
        "resolvedCommit": resolved_reference.resolved_commit,
        "satisfiesTraceability": resolved_reference.satisfies_traceability,
        "source": evidence.manifest.source,
        "sourceSha256": evidence.manifest.source_sha256,
        "state": reference.state.value,
    }
    if reference.provider is not None:
        payload["provider"] = reference.provider.as_dict()
    return FeatureGraphFact.create("pr-evidence", repository_id, payload)


def _extend_repository(
    *,
    base: MultiRepoFeatureGraph,
    repository_id: str,
    repository_root: Path,
    feature_id: str,
    trace_nodes: set[str],
    nodes: list[MultiRepoFeatureNode],
    edges: list[MultiRepoFeatureEdge],
    findings: list[FeatureGraphFinding],
    participant: bool,
) -> None:
    try:
        evidence = resolve_pull_request_evidence(repository_root, feature_id, repository_id)
    except PullRequestEvidenceError as exc:
        _finding(
            findings,
            FeatureGraphFindingLevel.ERROR,
            _error_code(exc),
            "repository PR evidence is invalid, conflicting, or scoped to another feature/repository",
            f"repository:{repository_id}",
            repository_id,
        )
        return
    if evidence is None:
        if participant:
            _finding(
                findings,
                FeatureGraphFindingLevel.WARNING,
                f"{_PR_ERROR_PREFIX}-MISSING",
                "repository has feature trace/ownership facts but no explicit PR evidence",
                f"repository:{repository_id}",
                repository_id,
            )
        return

    for resolved_reference in evidence.references:
        reference = resolved_reference.reference
        node_id = _pr_node_id(repository_id, reference.id)
        nodes.append(
            MultiRepoFeatureNode(
                node_id,
                FeatureGraphNodeType.PR_REFERENCE,
                (_pr_fact(repository_id, evidence, resolved_reference),),
            )
        )

        if not resolved_reference.commit_exists:
            _finding(
                findings,
                FeatureGraphFindingLevel.ERROR,
                f"{_PR_ERROR_PREFIX}-UNREACHABLE",
                "PR evidence headCommit is not available as a local commit object",
                node_id,
                repository_id,
            )
            continue
        if not resolved_reference.commit_reachable:
            _finding(
                findings,
                FeatureGraphFindingLevel.ERROR,
                f"{_PR_ERROR_PREFIX}-STALE",
                "PR evidence headCommit is not reachable from the repository's current HEAD",
                node_id,
                repository_id,
            )
            continue
        if reference.state is PullRequestState.CLOSED:
            _finding(
                findings,
                FeatureGraphFindingLevel.WARNING,
                f"{_PR_ERROR_PREFIX}-CLOSED",
                "closed PR evidence is retained for provenance but does not satisfy traceability",
                node_id,
                repository_id,
            )
            continue

        missing_links = tuple(sorted(link for link in reference.links if link not in trace_nodes))
        if missing_links:
            _finding(
                findings,
                FeatureGraphFindingLevel.ERROR,
                f"{_PR_ERROR_PREFIX}-STALE-LINK",
                "PR evidence references trace nodes that are absent from this repository's current feature trace",
                node_id,
                repository_id,
            )
            continue

        for link in reference.links:
            edges.append(
                MultiRepoFeatureEdge(
                    "included-in-pr",
                    link,
                    node_id,
                    (
                        FeatureGraphFact.create(
                            "pr-trace-link",
                            repository_id,
                            {
                                "evidenceSha256": evidence.sha256,
                                "headCommit": reference.head_commit,
                                "localId": reference.id,
                                "manifestSha256": evidence.manifest.sha256,
                                "source": evidence.manifest.source,
                                "sourceSha256": evidence.manifest.source_sha256,
                            },
                        ),
                    ),
                )
            )


def build_multi_repo_feature_graph(
    project_root: Path,
    feature_id: str,
) -> MultiRepoFeatureGraph:
    """Build the 0.15 graph and extend it with explicit local PR evidence.

    This function is read-only and provider-neutral. It never performs network
    calls or Git mutations. Provider names/URLs/references are optional display
    metadata and never participate in canonical PR node identity.
    """
    root = Path(project_root).resolve()
    base = build_base_multi_repo_feature_graph(root, feature_id)
    nodes = list(base.nodes)
    edges = list(base.edges)
    findings = list(base.findings)
    trace_by_repository = _repository_trace_nodes(base)
    participants = _participant_repositories(base)

    try:
        repositories = resolve_feature_repositories(root)
    except FeatureRepositoryError:
        return base

    for repository in repositories.repositories:
        if repository.root is None:
            continue
        repository_id = repository.repository.id
        _extend_repository(
            base=base,
            repository_id=repository_id,
            repository_root=repository.root,
            feature_id=feature_id,
            trace_nodes=trace_by_repository.get(repository_id, set()),
            nodes=nodes,
            edges=edges,
            findings=findings,
            participant=repository_id in participants,
        )

    return MultiRepoFeatureGraph(
        feature_id=base.feature_id,
        nodes=tuple(nodes),
        edges=tuple(edges),
        findings=tuple(findings),
        store_resolution_sha256=base.store_resolution_sha256,
        repository_resolution_sha256=base.repository_resolution_sha256,
    )


__all__ = ["build_multi_repo_feature_graph"]
