from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess

from sdai.artifact_state import record_artifact_state
from sdai.convergence import (
    ConvergenceStatus,
    EscalationReason,
    RemediationKind,
    convergence_state_path,
    load_convergence_state,
    run_convergence,
)
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance
from sdai.verification import SemanticReviewDimension, SemanticReviewEvidence
from sdai.version_entrypoint import main


FEATURE = "CONVERGE-120"


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


def _review_path(root: Path, review_id: str) -> Path:
    return root / ".sdai" / "verification" / FEATURE / "reviews" / f"{review_id}.json"


def _context_path(root: Path, name: str) -> Path:
    return root / ".sdai" / "verification" / FEATURE / "context" / f"{name}.txt"


def _workspace(root: Path, *, include_failure_review: bool = False) -> str:
    _git(root, "init")
    _git(root, "config", "user.email", "sdai@example.test")
    _git(root, "config", "user.name", "SDAI Convergence Test")
    _write(root / ".sdai" / "config.yaml", "version: 1\n")
    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """# Requirements

- FR-001: Sign café scripts. TASK-001 implements it and TEST-001 verifies it.
- NFR-001: Signing must preserve Δ behavior. TASK-001 implements it and TEST-001 verifies it.
""",
    )
    _write(
        feature / "architecture.md",
        "# Architecture\n\nThe signing component implements FR-001 and NFR-001.\n",
    )
    _write(
        feature / "plan.md",
        "# Plan\n\nImplement TASK-001 for FR-001 and NFR-001, then run TEST-001.\n",
    )
    _write(
        feature / "tasks.md",
        "# Tasks\n\n- [x] TASK-001: Implement FR-001 and NFR-001; verified by TEST-001.\n",
    )
    _write(
        feature / "tests.md",
        "# Tests\n\n- TEST-001: Verify FR-001 and NFR-001 through TASK-001.\n",
    )
    _write(
        root / "src" / "signing" / "café.py",
        "# Trace: FR-001 NFR-001 TASK-001 TEST-001\nSIGNED = True\n",
    )
    _write(
        root / "tests" / "test_signing.py",
        "# Trace: FR-001 NFR-001 TASK-001 TEST-001\ndef test_signing():\n    assert True\n",
    )
    for name in ("FR-001", "NFR-001", "failure"):
        _write(_context_path(root, name), f"Semantic context for {name} café Δ.\n")

    for artifact_id in ("requirements", "architecture", "plan", "tasks", "tests"):
        record_artifact_state(root, FEATURE, artifact_id, risk="standard", environ={})

    _git(root, "add", ".")
    _git(root, "commit", "-m", "convergence baseline")
    commit = _git(root, "rev-parse", "HEAD")

    for requirement in ("FR-001", "NFR-001"):
        _typed_evidence(
            root,
            commit,
            evidence_id=f"EVIDENCE-{requirement}",
            subject=f"requirement:{requirement}",
        )
        _semantic_review(
            root,
            commit,
            review_id=f"REVIEW-{requirement}",
            dimension=SemanticReviewDimension.REQUIREMENT_SATISFACTION,
            subject=f"requirement:{requirement}",
            context_name=requirement,
        )
    if include_failure_review:
        _add_failure_review(root, commit)
    return commit


def _typed_evidence(root: Path, commit: str, *, evidence_id: str, subject: str) -> Path:
    relative = f"specs/changes/{FEATURE}/evidence/{evidence_id}.json"
    record = TraceEvidence(
        evidence_id=evidence_id,
        kind=EvidenceKind.TEST,
        status=EvidenceStatus.PASSED,
        subject=subject,
        git_commit=commit,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.SOURCE,
                "src/signing/café.py",
                _digest(root / "src" / "signing" / "café.py"),
            ),
        ),
        provenance=(TraceProvenance(relative, 1),),
        producer=EvidenceProducer("tester", "codex", "model-a"),
        result={"passed": True},
        command=("pytest", "-q"),
        tool="pytest",
    )
    return _write(root / relative, record.to_json())


def _semantic_review(
    root: Path,
    commit: str,
    *,
    review_id: str,
    dimension: SemanticReviewDimension,
    subject: str,
    context_name: str,
) -> Path:
    path = _review_path(root, review_id)
    context = _context_path(root, context_name)
    relative = path.relative_to(root).as_posix()
    evidence = TraceEvidence(
        evidence_id=review_id,
        kind=EvidenceKind.REVIEW,
        status=EvidenceStatus.PASSED,
        subject=subject,
        git_commit=commit,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.EVIDENCE,
                context.relative_to(root).as_posix(),
                _digest(context),
            ),
        ),
        provenance=(TraceProvenance(relative, 1),),
        producer=EvidenceProducer("reviewer", "codex", "model-a"),
        result={"verdict": "passed", "dimension": dimension.value},
        command=(),
        tool="semantic-review",
    )
    review = SemanticReviewEvidence(
        review_id=review_id,
        dimension=dimension,
        subject=subject,
        summary=f"{dimension.value} review for {subject} passed.",
        evidence=evidence,
    )
    return _write(path, review.to_json())


def _add_failure_review(root: Path, commit: str | None = None) -> Path:
    resolved_commit = commit or _git(root, "rev-parse", "HEAD")
    return _semantic_review(
        root,
        resolved_commit,
        review_id="REVIEW-FAILURE",
        dimension=SemanticReviewDimension.FAILURE_BEHAVIOR,
        subject=f"feature:{FEATURE}",
        context_name="failure",
    )


def _convergence_snapshot(root: Path) -> dict[str, bytes]:
    directory = root / ".sdai" / "convergence" / FEATURE
    if not directory.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_first_convergence_creates_deterministic_review_task_without_touching_spec_truth(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    requirements = tmp_path / "specs" / "changes" / FEATURE / "requirements.md"
    before_requirements = requirements.read_bytes()

    state = run_convergence(tmp_path, FEATURE, risk="standard", max_rounds=3, environ={})

    assert state.status is ConvergenceStatus.ACTION_REQUIRED
    assert state.escalation_reason is None
    assert len(state.rounds) == 1
    assert len(state.tasks) == 1
    task = state.tasks[0]
    assert task.remediation_kind is RemediationKind.REVIEW
    assert task.finding_code == "SDAI_VERIFY_SEMANTIC_REQUIRED"
    assert task.subject == f"feature:{FEATURE}"
    assert f"specs/changes/{FEATURE}/requirements.md" in task.forbidden_roots
    assert "specs/current" in task.forbidden_roots
    assert all("requirements.md" not in root for root in task.allowed_roots)
    assert requirements.read_bytes() == before_requirements
    assert convergence_state_path(tmp_path, FEATURE).is_file()
    task_file = (
        tmp_path
        / ".sdai"
        / "convergence"
        / FEATURE
        / "tasks"
        / f"{task.task_id}.json"
    )
    assert task_file.is_file()
    assert json.loads(task_file.read_text(encoding="utf-8"))["apiVersion"] == "sdai.remediation-task/v1"


def test_same_verification_input_is_exactly_idempotent_and_does_not_rewrite_state(tmp_path: Path) -> None:
    _workspace(tmp_path)
    first = run_convergence(tmp_path, FEATURE, max_rounds=3, environ={})
    before = _convergence_snapshot(tmp_path)

    second = run_convergence(tmp_path, FEATURE, max_rounds=3, environ={})
    after = _convergence_snapshot(tmp_path)

    assert second == first
    assert second.sha256 == first.sha256
    assert len(second.rounds) == 1
    assert len(second.tasks) == 1
    assert after == before


def test_convergence_becomes_verified_after_current_review_is_added(tmp_path: Path) -> None:
    commit = _workspace(tmp_path)
    first = run_convergence(tmp_path, FEATURE, max_rounds=3, environ={})
    assert first.status is ConvergenceStatus.ACTION_REQUIRED

    _add_failure_review(tmp_path, commit)
    second = run_convergence(tmp_path, FEATURE, max_rounds=3, environ={})

    assert second.status is ConvergenceStatus.VERIFIED
    assert second.escalation_reason is None
    assert len(second.rounds) == 2
    assert second.rounds[-1].task_ids == ()
    assert len(second.tasks) == 1


def test_same_findings_on_new_git_input_escalate_no_progress_without_duplicate_tasks(tmp_path: Path) -> None:
    _workspace(tmp_path)
    first = run_convergence(tmp_path, FEATURE, max_rounds=3, environ={})
    assert first.status is ConvergenceStatus.ACTION_REQUIRED
    original_task_ids = tuple(task.task_id for task in first.tasks)

    _write(tmp_path / "notes.txt", "Unrelated commit changes HEAD but not verification findings.\n")
    _git(tmp_path, "add", "notes.txt")
    _git(tmp_path, "commit", "-m", "unrelated state change")
    second = run_convergence(tmp_path, FEATURE, max_rounds=3, environ={})

    assert second.status is ConvergenceStatus.ESCALATED
    assert second.escalation_reason is EscalationReason.NO_PROGRESS
    assert len(second.rounds) == 2
    assert second.rounds[-1].task_ids == ()
    assert tuple(task.task_id for task in second.tasks) == original_task_ids


def test_max_rounds_escalates_before_creating_another_round(tmp_path: Path) -> None:
    _workspace(tmp_path)
    first = run_convergence(tmp_path, FEATURE, max_rounds=1, environ={})
    assert first.status is ConvergenceStatus.ACTION_REQUIRED
    assert len(first.rounds) == 1

    _write(tmp_path / "notes.txt", "Another unrelated Git input.\n")
    _git(tmp_path, "add", "notes.txt")
    _git(tmp_path, "commit", "-m", "change verification input")
    second = run_convergence(tmp_path, FEATURE, max_rounds=1, environ={})

    assert second.status is ConvergenceStatus.ESCALATED
    assert second.escalation_reason is EscalationReason.MAX_ROUNDS
    assert len(second.rounds) == 1
    assert len(second.tasks) == len(first.tasks)


def test_stale_requirements_are_non_remediable_and_never_become_agent_tasks(tmp_path: Path) -> None:
    _workspace(tmp_path, include_failure_review=True)
    requirements = tmp_path / "specs" / "changes" / FEATURE / "requirements.md"
    _write(
        requirements,
        requirements.read_text(encoding="utf-8") + "\n<!-- requirement truth changed after baseline -->\n",
    )

    state = run_convergence(tmp_path, FEATURE, max_rounds=3, environ={})

    assert state.status is ConvergenceStatus.ESCALATED
    assert state.escalation_reason is EscalationReason.NON_REMEDIABLE
    assert state.tasks == ()
    assert state.rounds[-1].non_remediable
    assert any("ARTIFACT" in item for item in state.rounds[-1].non_remediable)


def test_cli_json_exit_semantics_and_help_are_stable(tmp_path: Path, capsys) -> None:
    _workspace(tmp_path)

    code = main(
        [
            "converge",
            FEATURE,
            "--risk",
            "standard",
            "--max-rounds",
            "3",
            "--json",
            "--path",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 3
    assert captured.err == ""
    assert payload["apiVersion"] == "sdai.convergence-state/v1"
    assert payload["status"] == "action-required"
    assert payload["tasks"][0]["remediation_kind"] == "review"

    assert main(["converge", "--help"]) == 0
    help_output = capsys.readouterr()
    assert "--max-rounds" in help_output.out
    assert help_output.err == ""


def test_cli_returns_zero_after_verification_converges(tmp_path: Path, capsys) -> None:
    commit = _workspace(tmp_path)
    assert main(["converge", FEATURE, "--path", str(tmp_path)]) == 3
    capsys.readouterr()
    _add_failure_review(tmp_path, commit)

    code = main(["converge", FEATURE, "--json", "--path", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "verified"


def test_corrupt_state_fails_closed_and_is_not_silently_replaced(tmp_path: Path, capsys) -> None:
    _workspace(tmp_path)
    assert main(["converge", FEATURE, "--path", str(tmp_path)]) == 3
    capsys.readouterr()
    path = convergence_state_path(tmp_path, FEATURE)
    _write(path, "{not-json\n")
    before = path.read_bytes()

    code = main(["converge", FEATURE, "--json", "--path", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "SDAI-CONVERGE-005" in captured.err
    assert path.read_bytes() == before


def test_existing_ledger_rejects_risk_or_bound_changes(tmp_path: Path) -> None:
    _workspace(tmp_path)
    run_convergence(tmp_path, FEATURE, risk="standard", max_rounds=3, environ={})

    try:
        run_convergence(tmp_path, FEATURE, risk="critical", max_rounds=3, environ={})
    except RuntimeError as exc:
        assert "cannot change risk" in str(exc)
    else:
        raise AssertionError("convergence ledger accepted a risk change")

    try:
        run_convergence(tmp_path, FEATURE, risk="standard", max_rounds=4, environ={})
    except RuntimeError as exc:
        assert "cannot change max_rounds" in str(exc)
    else:
        raise AssertionError("convergence ledger accepted a max_rounds change")


def test_persisted_state_round_trips_with_utf8_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "convergence Ω workspace"
    workspace.mkdir()
    _workspace(workspace)

    state = run_convergence(workspace, FEATURE, max_rounds=3, environ={})
    loaded = load_convergence_state(workspace, FEATURE)

    assert loaded == state
    assert loaded is not None
    assert loaded.sha256 == state.sha256
    assert "café" not in loaded.to_json() or loaded.tasks[0].provenance
