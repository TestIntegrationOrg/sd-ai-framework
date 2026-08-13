from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess

import pytest

from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance
from sdai.trace_policy import (
    CoverageDimension,
    TracePolicyError,
    evaluate_trace_policy,
    resolve_trace_policy,
)
from sdai.version_entrypoint import main


FEATURE = "TRACE-108"


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


def _repo(root: Path, *, second_requirement: bool = False) -> str:
    _git(root, "init")
    _git(root, "config", "user.email", "sdai@example.test")
    _git(root, "config", "user.name", "SDAI Test")
    _write(root / ".sdai" / "config.yaml", "version: 1\n")
    feature = root / "specs" / "changes" / FEATURE
    extra = "- FR-002: A second requirement without implementation.\n" if second_requirement else ""
    _write(
        feature / "requirements.md",
        f"""# Requirements

- FR-001: Sign café scripts.
{extra}""",
    )
    _write(
        feature / "tasks.md",
        "- TASK-001: Implement signing for FR-001.\n",
    )
    _write(
        feature / "tests.md",
        "- TEST-001: Verify signing for FR-001.\n",
    )
    _write(
        root / "src" / "café.py",
        "# Trace: FR-001 TASK-001 TEST-001\nVALUE = 1\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    return _git(root, "rev-parse", "HEAD")


def _evidence(root: Path, commit: str, kind: EvidenceKind, evidence_id: str) -> Path:
    source = root / "src" / "café.py"
    relative = f"specs/changes/{FEATURE}/evidence/{evidence_id}.json"
    record = TraceEvidence(
        evidence_id=evidence_id,
        kind=kind,
        status=EvidenceStatus.PASSED,
        subject="requirement:FR-001",
        git_commit=commit,
        bindings=(
            EvidenceBinding(EvidenceBindingKind.SOURCE, "src/café.py", _digest(source)),
        ),
        provenance=(TraceProvenance(relative, 1),),
        producer=EvidenceProducer("tester", "codex", "model-a"),
        result={"passed": True},
        command=("python", "-m", "pytest"),
        tool="pytest",
    )
    return _write(root / relative, record.to_json())


def _all_evidence(root: Path, commit: str) -> None:
    _evidence(root, commit, EvidenceKind.TEST, "EVIDENCE-TEST")
    _evidence(root, commit, EvidenceKind.SECURITY, "EVIDENCE-SECURITY")
    _evidence(root, commit, EvidenceKind.APPROVAL, "EVIDENCE-APPROVAL")


def _policy(path: Path, policy_id: str, risk: str, **dimensions: float) -> Path:
    lines = [
        "apiVersion: sdai.trace-policy/v1",
        "kind: TraceCoveragePolicy",
        "metadata:",
        f"  id: {policy_id}",
        "spec:",
        "  risks:",
        f"    {risk}:",
    ]
    for key, value in dimensions.items():
        lines.append(f"      {key}: {value}")
    return _write(path, "\n".join(lines) + "\n")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def _by_dimension(report):
    return {item.dimension: item for item in report.dimensions}


def test_standard_policy_passes_with_current_task_code_and_test_coverage(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    _evidence(tmp_path, commit, EvidenceKind.TEST, "EVIDENCE-TEST")

    report = evaluate_trace_policy(tmp_path, FEATURE, "standard", environ={})
    dimensions = _by_dimension(report)

    assert report.passed is True
    assert dimensions[CoverageDimension.REQUIREMENTS].actual_percent == 100.0
    assert dimensions[CoverageDimension.TASKS].actual_percent == 100.0
    assert dimensions[CoverageDimension.CODE].actual_percent == 100.0
    assert dimensions[CoverageDimension.TESTS].actual_percent == 100.0
    assert dimensions[CoverageDimension.SECURITY].threshold.required_percent == 0.0
    assert dimensions[CoverageDimension.APPROVALS].threshold.required_percent == 0.0


def test_critical_defaults_require_one_hundred_percent_across_all_dimensions(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    _all_evidence(tmp_path, commit)

    report = evaluate_trace_policy(tmp_path, FEATURE, "critical", environ={})

    assert report.passed is True
    assert all(item.threshold.required_percent == 100.0 for item in report.dimensions)
    assert all(item.actual_percent == 100.0 for item in report.dimensions)


def test_critical_policy_blocks_when_any_requirement_is_uncovered(tmp_path: Path) -> None:
    commit = _repo(tmp_path, second_requirement=True)
    _all_evidence(tmp_path, commit)

    report = evaluate_trace_policy(tmp_path, FEATURE, "critical", environ={})
    dimensions = _by_dimension(report)

    assert report.passed is False
    assert dimensions[CoverageDimension.REQUIREMENTS].actual_percent == 50.0
    assert dimensions[CoverageDimension.TASKS].actual_percent == 50.0
    assert dimensions[CoverageDimension.CODE].actual_percent == 50.0
    assert dimensions[CoverageDimension.TESTS].actual_percent == 50.0
    assert dimensions[CoverageDimension.SECURITY].actual_percent == 50.0
    assert dimensions[CoverageDimension.APPROVALS].actual_percent == 50.0
    assert {finding.dimension for finding in report.findings} == set(CoverageDimension)
    assert all(finding.severity == "blocking" for finding in report.findings)


def test_stale_evidence_never_counts_in_policy_numerators(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    _all_evidence(tmp_path, commit)
    _write(tmp_path / "src" / "café.py", "# Trace: FR-001 TASK-001 TEST-001\nVALUE = 2\n")

    report = evaluate_trace_policy(tmp_path, FEATURE, "critical", environ={})
    dimensions = _by_dimension(report)

    assert report.passed is False
    assert dimensions[CoverageDimension.REQUIREMENTS].actual_percent == 0.0
    assert dimensions[CoverageDimension.SECURITY].actual_percent == 0.0
    assert dimensions[CoverageDimension.APPROVALS].actual_percent == 0.0
    # Structural links remain current graph facts even though proof bytes are stale.
    assert dimensions[CoverageDimension.TASKS].actual_percent == 100.0
    assert dimensions[CoverageDimension.CODE].actual_percent == 100.0
    assert dimensions[CoverageDimension.TESTS].actual_percent == 100.0


def test_org_minimum_cannot_be_weakened_by_repo_or_user_policy(tmp_path: Path) -> None:
    _repo(tmp_path)
    org = _policy(tmp_path / "external" / "org.yaml", "org-policy", "standard", requirements=95)
    _policy(tmp_path / ".sdai" / "trace-policy.yaml", "repo-policy", "standard", requirements=10)
    user = _policy(tmp_path / "external" / "user.yaml", "user-policy", "standard", requirements=20)

    thresholds, sources = resolve_trace_policy(
        tmp_path,
        "standard",
        environ={
            "SDAI_ORG_TRACE_POLICY_PATH": str(org.resolve()),
            "SDAI_USER_TRACE_POLICY_PATH": str(user.resolve()),
        },
    )
    by_dimension = {item.dimension: item for item in thresholds}
    requirement = by_dimension[CoverageDimension.REQUIREMENTS]

    assert requirement.required_percent == 95.0
    assert [item.layer.value for item in requirement.contributions] == [
        "builtin",
        "org",
        "repo",
        "user",
    ]
    assert [item.value for item in requirement.contributions] == [80.0, 95.0, 10.0, 20.0]
    assert requirement.as_dict()["enforced_by"][0]["layer"] == "org"
    assert any(source.startswith("org:") for source in sources)
    assert any(source.startswith("repo:") for source in sources)
    assert any(source.startswith("user:") for source in sources)


def test_org_can_strengthen_standard_security_and_approval_requirements(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    _evidence(tmp_path, commit, EvidenceKind.TEST, "EVIDENCE-TEST")
    org = _policy(
        tmp_path / "external" / "org.yaml",
        "org-secure",
        "standard",
        security=100,
        approvals=100,
    )

    report = evaluate_trace_policy(
        tmp_path,
        FEATURE,
        "standard",
        environ={"SDAI_ORG_TRACE_POLICY_PATH": str(org.resolve())},
    )
    dimensions = _by_dimension(report)

    assert report.passed is False
    assert dimensions[CoverageDimension.SECURITY].threshold.required_percent == 100.0
    assert dimensions[CoverageDimension.APPROVALS].threshold.required_percent == 100.0
    assert {item.dimension for item in report.findings} == {
        CoverageDimension.SECURITY,
        CoverageDimension.APPROVALS,
    }


def test_invalid_policy_threshold_fails_closed(tmp_path: Path) -> None:
    _repo(tmp_path)
    _policy(tmp_path / ".sdai" / "trace-policy.yaml", "bad-policy", "standard", requirements=101)

    with pytest.raises(TracePolicyError, match="SDAI-TRACE-POLICY-002"):
        resolve_trace_policy(tmp_path, "standard", environ={})


def test_trace_policy_cli_has_machine_clean_json_blocking_exit_and_provenance(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    commit = _repo(tmp_path)
    _evidence(tmp_path, commit, EvidenceKind.TEST, "EVIDENCE-TEST")
    org = _policy(
        tmp_path / "external" / "org.yaml",
        "org-secure",
        "standard",
        security=100,
    )
    monkeypatch.setenv("SDAI_ORG_TRACE_POLICY_PATH", str(org.resolve()))

    exit_code = main(
        ["trace", "policy", FEATURE, "--risk", "standard", "--json", "--path", str(tmp_path)]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert payload["apiVersion"] == "sdai.trace-policy-report/v1"
    assert payload["passed"] is False
    security = next(item for item in payload["dimensions"] if item["dimension"] == "security")
    assert security["required_percent"] == 100.0
    assert security["threshold"]["enforced_by"][0]["layer"] == "org"
    assert payload["findings"][0]["severity"] == "blocking"


def test_policy_evaluation_is_read_only_and_utf8_portable(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    _all_evidence(tmp_path, commit)
    before = _snapshot(tmp_path)

    first = evaluate_trace_policy(tmp_path, FEATURE, "critical", environ={})
    second = evaluate_trace_policy(tmp_path, FEATURE, "critical", environ={})

    assert first.to_json() == second.to_json()
    assert "café.py" not in first.to_json() or first.graph_sha256.startswith("sha256:")
    assert _snapshot(tmp_path) == before
