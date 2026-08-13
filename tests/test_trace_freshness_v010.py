from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess

import pytest

from sdai.artifact_state import (
    ArtifactFreshness,
    ArtifactState,
    ArtifactStateReport,
)
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_freshness import (
    CommitPolicy,
    ProofFreshness,
    TraceFreshnessError,
    evaluate_trace_coverage,
    evaluate_trace_evidence_file,
    evaluate_trace_evidence_freshness,
)
from sdai.trace_graph import (
    TraceEdge,
    TraceGraph,
    TraceNode,
    TraceNodeType,
    TraceProvenance,
    TraceRelation,
)


FEATURE = "TRACE-106"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=True,
        shell=False,
    )
    return result.stdout.strip()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _digest(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def _repo(root: Path) -> str:
    _git(root, "init")
    _git(root, "config", "user.email", "sdai@example.test")
    _git(root, "config", "user.name", "SDAI Test")
    _write(root / "src" / "café.py", "# FR-001\nVALUE = 1\n")
    _write(root / "tests" / "test_café.py", "# FR-001\ndef test_value():\n    assert True\n")
    _write(
        root / "specs" / "changes" / FEATURE / "contracts" / "api.yaml",
        "id: CONTRACT-001\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return _git(root, "rev-parse", "HEAD")


def _evidence(
    root: Path,
    commit: str,
    *,
    status: EvidenceStatus = EvidenceStatus.PASSED,
    bindings: tuple[EvidenceBinding, ...] | None = None,
) -> TraceEvidence:
    effective = bindings or (
        EvidenceBinding(
            EvidenceBindingKind.SOURCE,
            "src/café.py",
            _digest(root / "src" / "café.py"),
        ),
        EvidenceBinding(
            EvidenceBindingKind.TEST,
            "tests/test_café.py",
            _digest(root / "tests" / "test_café.py"),
        ),
        EvidenceBinding(
            EvidenceBindingKind.ARTIFACT,
            f"specs/changes/{FEATURE}/contracts/api.yaml",
            _digest(root / "specs" / "changes" / FEATURE / "contracts" / "api.yaml"),
        ),
    )
    return TraceEvidence(
        evidence_id="EVIDENCE-001",
        kind=EvidenceKind.TEST,
        status=status,
        subject="requirement:FR-001",
        git_commit=commit,
        bindings=effective,
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/evidence/test.json",
                1,
            ),
        ),
        producer=EvidenceProducer("tester", "codex", "model-a"),
        result={"passed": 1},
        command=("python", "-m", "pytest"),
        tool="pytest",
    )


def test_current_or_ancestor_commit_with_matching_bindings_is_valid(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    record = _evidence(tmp_path, commit)

    report = evaluate_trace_evidence_freshness(tmp_path, record)

    assert report.freshness is ProofFreshness.VALID
    assert report.satisfies_current_coverage is True
    assert report.commit_reachable is True
    assert {item.freshness for item in report.bindings} == {ProofFreshness.VALID}

    _write(tmp_path / "README.md", "later unrelated change\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "unrelated")
    ancestor = evaluate_trace_evidence_freshness(tmp_path, record)
    assert ancestor.freshness is ProofFreshness.VALID
    assert ancestor.current_git_commit != commit


def test_exact_head_policy_invalidates_older_commit_even_if_bound_bytes_match(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    record = _evidence(tmp_path, commit)
    _write(tmp_path / "README.md", "new\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "later")

    report = evaluate_trace_evidence_freshness(
        tmp_path,
        record,
        commit_policy=CommitPolicy.EXACT_HEAD,
    )

    assert report.freshness is ProofFreshness.STALE
    assert report.satisfies_current_coverage is False


def test_changed_source_and_changed_contract_invalidate_evidence(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    record = _evidence(tmp_path, commit)

    _write(tmp_path / "src" / "café.py", "# FR-001\nVALUE = 2\n")
    changed_source = evaluate_trace_evidence_freshness(tmp_path, record)
    assert changed_source.freshness is ProofFreshness.STALE
    assert any(
        item.source == "src/café.py" and item.freshness is ProofFreshness.STALE
        for item in changed_source.bindings
    )

    _write(tmp_path / "src" / "café.py", "# FR-001\nVALUE = 1\n")
    _write(
        tmp_path / "specs" / "changes" / FEATURE / "contracts" / "api.yaml",
        "id: CONTRACT-001\nversion: 2\n",
    )
    changed_contract = evaluate_trace_evidence_freshness(tmp_path, record)
    assert changed_contract.freshness is ProofFreshness.STALE
    assert any(
        item.kind == "artifact" and item.freshness is ProofFreshness.STALE
        for item in changed_contract.bindings
    )


def test_deleted_test_is_missing_and_never_satisfies_coverage(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    record = _evidence(tmp_path, commit)
    (tmp_path / "tests" / "test_café.py").unlink()

    report = evaluate_trace_evidence_freshness(tmp_path, record)

    assert report.freshness is ProofFreshness.MISSING
    assert report.satisfies_current_coverage is False
    assert any(
        item.kind == "test" and item.current_sha256 is None
        for item in report.bindings
    )


def test_disconnected_rewritten_history_commit_is_stale(tmp_path: Path) -> None:
    original = _repo(tmp_path)
    original_branch = _git(tmp_path, "branch", "--show-current")
    _git(tmp_path, "checkout", "--orphan", "rewritten-history")
    _git(tmp_path, "rm", "-rf", ".")
    _write(tmp_path / "orphan.txt", "disconnected\n")
    _git(tmp_path, "add", "orphan.txt")
    _git(tmp_path, "commit", "-m", "orphan")
    orphan = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", original_branch)
    assert _git(tmp_path, "rev-parse", "HEAD") == original

    report = evaluate_trace_evidence_freshness(tmp_path, _evidence(tmp_path, orphan))

    assert report.commit_reachable is False
    assert report.freshness is ProofFreshness.STALE


def test_blocked_and_failed_evidence_never_satisfy_current_coverage(tmp_path: Path) -> None:
    commit = _repo(tmp_path)

    blocked = evaluate_trace_evidence_freshness(
        tmp_path,
        _evidence(tmp_path, commit, status=EvidenceStatus.BLOCKED),
    )
    failed = evaluate_trace_evidence_freshness(
        tmp_path,
        _evidence(tmp_path, commit, status=EvidenceStatus.FAILED),
    )

    assert blocked.freshness is ProofFreshness.BLOCKED
    assert failed.freshness is ProofFreshness.BLOCKED
    assert blocked.satisfies_current_coverage is False
    assert failed.satisfies_current_coverage is False


def test_stale_08_artifact_state_invalidates_matching_artifact_binding(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    record = _evidence(tmp_path, commit)
    artifact_path = f"specs/changes/{FEATURE}/contracts/api.yaml"
    state = ArtifactState(
        artifact_id="contracts",
        path=artifact_path,
        required=True,
        freshness=ArtifactFreshness.STALE,
        current_sha256=_digest(tmp_path / artifact_path),
        recorded_sha256=_digest(tmp_path / artifact_path),
        definition_sha256="sha256:" + "2" * 64,
        recorded_definition_sha256="sha256:" + "1" * 64,
        dependencies=(),
        reasons=("definition changed",),
        evidence=(),
        record_source=f"specs/changes/{FEATURE}/.sdai/artifact-state/contracts.yaml",
    )
    artifact_report = ArtifactStateReport(
        feature_id=FEATURE,
        risk="standard",
        domain=None,
        states=(state,),
        topological_order=("contracts",),
    )

    report = evaluate_trace_evidence_freshness(
        tmp_path,
        record,
        artifact_state_report=artifact_report,
    )

    assert report.freshness is ProofFreshness.STALE
    assert any("0.8 artifact state" in item.reason for item in report.bindings)


def test_missing_record_is_missing_but_corrupt_record_fails_closed(tmp_path: Path) -> None:
    _repo(tmp_path)
    missing = evaluate_trace_evidence_file(tmp_path, Path("missing.json"))
    assert missing.freshness is ProofFreshness.MISSING

    bad = _write(
        tmp_path / "specs" / "changes" / FEATURE / "evidence" / "bad.json",
        '{"apiVersion":"sdai.trace-evidence/v1","sha256":"bad"}',
    )
    with pytest.raises(TraceFreshnessError, match="corrupt or unsafe evidence"):
        evaluate_trace_evidence_file(tmp_path, bad)


def test_freshness_propagates_to_evidenced_by_coverage_edges(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    record = _evidence(tmp_path, commit)
    report = evaluate_trace_evidence_freshness(tmp_path, record)
    graph = TraceGraph(
        feature_id=FEATURE,
        nodes=(
            TraceNode(
                TraceNodeType.REQUIREMENT,
                "FR-001",
                provenance=(TraceProvenance("requirements.md", 1),),
            ),
            TraceNode(
                TraceNodeType.EVIDENCE,
                "EVIDENCE-001",
                provenance=(TraceProvenance("evidence.json", 1),),
            ),
        ),
        edges=(
            TraceEdge(
                TraceRelation.EVIDENCED_BY,
                "requirement:FR-001",
                "evidence:EVIDENCE-001",
                provenance=(TraceProvenance("evidence.json", 1),),
            ),
        ),
    )

    valid = evaluate_trace_coverage(graph, {"EVIDENCE-001": report})
    missing = evaluate_trace_coverage(graph, {})

    assert valid[0].satisfies_current_coverage is True
    assert missing[0].freshness is ProofFreshness.MISSING
    assert missing[0].satisfies_current_coverage is False
