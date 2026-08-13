from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
import subprocess
from typing import Mapping

from sdai.artifact_state import ArtifactFreshness, ArtifactStateReport
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceStatus,
    TraceEvidence,
    TraceEvidenceError,
    load_trace_evidence,
)
from sdai.trace_graph import TraceGraph, TraceRelation


class TraceFreshnessError(RuntimeError):
    """Raised when current trace proof cannot be evaluated safely."""


class CommitPolicy(str, Enum):
    ANCESTOR = "ancestor"
    EXACT_HEAD = "exact-head"


class ProofFreshness(str, Enum):
    VALID = "valid"
    STALE = "stale"
    MISSING = "missing"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class BindingFreshness:
    kind: str
    source: str
    recorded_sha256: str
    current_sha256: str | None
    freshness: ProofFreshness
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "source": self.source,
            "recorded_sha256": self.recorded_sha256,
            "current_sha256": self.current_sha256,
            "freshness": self.freshness.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EvidenceFreshnessReport:
    evidence_id: str | None
    subject: str | None
    freshness: ProofFreshness
    evidence_git_commit: str | None
    current_git_commit: str | None
    commit_policy: CommitPolicy
    commit_reachable: bool | None
    bindings: tuple[BindingFreshness, ...]
    reasons: tuple[str, ...]

    @property
    def satisfies_current_coverage(self) -> bool:
        return self.freshness is ProofFreshness.VALID

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "subject": self.subject,
            "freshness": self.freshness.value,
            "satisfies_current_coverage": self.satisfies_current_coverage,
            "evidence_git_commit": self.evidence_git_commit,
            "current_git_commit": self.current_git_commit,
            "commit_policy": self.commit_policy.value,
            "commit_reachable": self.commit_reachable,
            "bindings": [item.as_dict() for item in self.bindings],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class TraceCoverageProof:
    source_node_id: str
    evidence_node_id: str
    evidence_id: str
    freshness: ProofFreshness
    satisfies_current_coverage: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "source_node_id": self.source_node_id,
            "evidence_node_id": self.evidence_node_id,
            "evidence_id": self.evidence_id,
            "freshness": self.freshness.value,
            "satisfies_current_coverage": self.satisfies_current_coverage,
            "reasons": list(self.reasons),
        }


def _fail(code: str, message: str) -> TraceFreshnessError:
    return TraceFreshnessError(f"{code}: {message}")


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _safe_binding_path(root: Path, binding: EvidenceBinding) -> Path | None:
    candidate = root / Path(binding.source)
    try:
        safe = ensure_within_project(root, candidate, label="trace evidence binding")
    except PathSafetyError as exc:
        raise _fail(
            "SDAI-TRACE-FRESH-002",
            f"binding escapes project root: {binding.source!r}",
        ) from exc
    relative = safe.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise _fail(
                "SDAI-TRACE-FRESH-002",
                f"binding contains symlink component: {binding.source!r}",
            )
    if not safe.exists():
        return None
    if safe.is_symlink() or not safe.is_file():
        raise _fail(
            "SDAI-TRACE-FRESH-002",
            f"binding must resolve to a regular non-symlink file: {binding.source!r}",
        )
    return safe


def _git(root: Path, *args: str, allow_one: bool = False) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            shell=False,
        )
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-TRACE-FRESH-001", f"unable to execute git safely: {exc}") from exc
    if result.returncode != 0 and not (allow_one and result.returncode == 1):
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise _fail("SDAI-TRACE-FRESH-001", f"git command failed: {detail}")
    return result.returncode, result.stdout.strip()


def _git_state(root: Path, evidence_commit: str, policy: CommitPolicy) -> tuple[str, bool]:
    _, head = _git(root, "rev-parse", "--verify", "HEAD")
    normalized_head = head.casefold()
    if evidence_commit == normalized_head:
        return normalized_head, True
    if policy is CommitPolicy.EXACT_HEAD:
        return normalized_head, False

    exists_code, _ = _git(
        root,
        "cat-file",
        "-e",
        f"{evidence_commit}^{{commit}}",
        allow_one=True,
    )
    if exists_code != 0:
        return normalized_head, False
    ancestor_code, _ = _git(
        root,
        "merge-base",
        "--is-ancestor",
        evidence_commit,
        normalized_head,
        allow_one=True,
    )
    return normalized_head, ancestor_code == 0


def _artifact_freshness_by_path(report: ArtifactStateReport | None) -> Mapping[str, ArtifactFreshness]:
    if report is None:
        return {}
    return {state.path: state.freshness for state in report.states}


def _binding_state(
    root: Path,
    binding: EvidenceBinding,
    artifact_states: Mapping[str, ArtifactFreshness],
) -> BindingFreshness:
    path = _safe_binding_path(root, binding)
    if path is None:
        return BindingFreshness(
            kind=binding.kind.value,
            source=binding.source,
            recorded_sha256=binding.sha256,
            current_sha256=None,
            freshness=ProofFreshness.MISSING,
            reason="bound file is missing",
        )
    try:
        current = _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise _fail(
            "SDAI-TRACE-FRESH-002",
            f"unable to read binding {binding.source!r}: {exc}",
        ) from exc

    if binding.kind is EvidenceBindingKind.ARTIFACT:
        state = artifact_states.get(binding.source)
        if state is ArtifactFreshness.MISSING:
            freshness = ProofFreshness.MISSING
            reason = "0.8 artifact state reports the bound artifact missing"
        elif state is ArtifactFreshness.BLOCKED:
            freshness = ProofFreshness.BLOCKED
            reason = "0.8 artifact state reports the bound artifact blocked"
        elif state is ArtifactFreshness.STALE:
            freshness = ProofFreshness.STALE
            reason = "0.8 artifact state reports the bound artifact stale"
        elif current != binding.sha256:
            freshness = ProofFreshness.STALE
            reason = "bound file SHA-256 changed"
        else:
            freshness = ProofFreshness.VALID
            reason = "bound file SHA-256 matches current bytes"
    elif current != binding.sha256:
        freshness = ProofFreshness.STALE
        reason = "bound file SHA-256 changed"
    else:
        freshness = ProofFreshness.VALID
        reason = "bound file SHA-256 matches current bytes"

    return BindingFreshness(
        kind=binding.kind.value,
        source=binding.source,
        recorded_sha256=binding.sha256,
        current_sha256=current,
        freshness=freshness,
        reason=reason,
    )


def _overall_status(
    record: TraceEvidence,
    commit_reachable: bool,
    bindings: tuple[BindingFreshness, ...],
) -> tuple[ProofFreshness, tuple[str, ...]]:
    reasons: list[str] = []
    if not commit_reachable:
        reasons.append("evidence Git commit is not valid under the current commit policy")
    if record.status is EvidenceStatus.BLOCKED:
        reasons.append("evidence record is explicitly blocked")
    for binding in bindings:
        if binding.freshness is not ProofFreshness.VALID:
            reasons.append(f"{binding.source}: {binding.reason}")

    if any(item.freshness is ProofFreshness.MISSING for item in bindings):
        return ProofFreshness.MISSING, tuple(reasons)
    if record.status is EvidenceStatus.BLOCKED or any(
        item.freshness is ProofFreshness.BLOCKED for item in bindings
    ):
        return ProofFreshness.BLOCKED, tuple(reasons)
    if not commit_reachable or any(
        item.freshness is ProofFreshness.STALE for item in bindings
    ):
        return ProofFreshness.STALE, tuple(reasons)
    if record.status is EvidenceStatus.FAILED:
        return ProofFreshness.BLOCKED, ("evidence record reports failure",)
    return ProofFreshness.VALID, ("evidence commit and all bound content match current repository state",)


def evaluate_trace_evidence_freshness(
    project_root: Path,
    evidence: TraceEvidence,
    *,
    commit_policy: CommitPolicy = CommitPolicy.ANCESTOR,
    artifact_state_report: ArtifactStateReport | None = None,
) -> EvidenceFreshnessReport:
    """Evaluate whether validated evidence can satisfy coverage for current repository state."""
    root = project_root.resolve()
    if not isinstance(evidence, TraceEvidence):
        raise _fail("SDAI-TRACE-FRESH-003", "evidence must be a validated TraceEvidence record")
    try:
        policy = commit_policy if isinstance(commit_policy, CommitPolicy) else CommitPolicy(commit_policy)
    except ValueError as exc:
        raise _fail("SDAI-TRACE-FRESH-003", f"unsupported commit policy: {commit_policy!r}") from exc

    head, reachable = _git_state(root, evidence.git_commit, policy)
    artifact_states = _artifact_freshness_by_path(artifact_state_report)
    bindings = tuple(
        sorted(
            (_binding_state(root, item, artifact_states) for item in evidence.bindings),
            key=lambda item: (item.kind, item.source.casefold(), item.source),
        )
    )
    freshness, reasons = _overall_status(evidence, reachable, bindings)
    return EvidenceFreshnessReport(
        evidence_id=evidence.evidence_id,
        subject=evidence.subject,
        freshness=freshness,
        evidence_git_commit=evidence.git_commit,
        current_git_commit=head,
        commit_policy=policy,
        commit_reachable=reachable,
        bindings=bindings,
        reasons=reasons,
    )


def evaluate_trace_evidence_file(
    project_root: Path,
    path: Path,
    *,
    commit_policy: CommitPolicy = CommitPolicy.ANCESTOR,
    artifact_state_report: ArtifactStateReport | None = None,
) -> EvidenceFreshnessReport:
    """Load and evaluate one durable evidence record; missing proof remains explicit."""
    root = project_root.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        safe = ensure_within_project(root, candidate, label="trace evidence")
    except PathSafetyError as exc:
        raise _fail("SDAI-TRACE-FRESH-003", "evidence path must remain inside project root") from exc
    if not safe.exists():
        policy = commit_policy if isinstance(commit_policy, CommitPolicy) else CommitPolicy(commit_policy)
        return EvidenceFreshnessReport(
            evidence_id=None,
            subject=None,
            freshness=ProofFreshness.MISSING,
            evidence_git_commit=None,
            current_git_commit=None,
            commit_policy=policy,
            commit_reachable=None,
            bindings=(),
            reasons=("typed evidence record is missing",),
        )
    try:
        record = load_trace_evidence(root, safe)
    except TraceEvidenceError as exc:
        raise _fail("SDAI-TRACE-FRESH-003", f"corrupt or unsafe evidence record: {exc}") from exc
    return evaluate_trace_evidence_freshness(
        root,
        record,
        commit_policy=commit_policy,
        artifact_state_report=artifact_state_report,
    )


def evaluate_trace_coverage(
    graph: TraceGraph,
    reports: Mapping[str, EvidenceFreshnessReport],
) -> tuple[TraceCoverageProof, ...]:
    """Project evidence freshness onto evidenced-by graph edges without mutating the graph."""
    proofs: list[TraceCoverageProof] = []
    nodes = {node.node_id: node for node in graph.nodes}
    for edge in graph.edges:
        if edge.relation is not TraceRelation.EVIDENCED_BY:
            continue
        evidence_node = nodes.get(edge.target)
        if evidence_node is None:
            raise _fail(
                "SDAI-TRACE-FRESH-004",
                f"evidence edge target is missing from graph: {edge.target}",
            )
        evidence_id = evidence_node.entity_id
        report = reports.get(evidence_id)
        if report is None:
            proofs.append(
                TraceCoverageProof(
                    source_node_id=edge.source,
                    evidence_node_id=edge.target,
                    evidence_id=evidence_id,
                    freshness=ProofFreshness.MISSING,
                    satisfies_current_coverage=False,
                    reasons=("no current freshness report exists for evidence node",),
                )
            )
            continue
        proofs.append(
            TraceCoverageProof(
                source_node_id=edge.source,
                evidence_node_id=edge.target,
                evidence_id=evidence_id,
                freshness=report.freshness,
                satisfies_current_coverage=report.satisfies_current_coverage,
                reasons=report.reasons,
            )
        )
    return tuple(
        sorted(
            proofs,
            key=lambda item: (item.source_node_id, item.evidence_node_id, item.evidence_id),
        )
    )
