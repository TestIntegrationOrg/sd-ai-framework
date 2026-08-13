from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from sdai.trace_builder import build_feature_trace_graph
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceNodeType, TraceProvenance
from sdai.trace_policy import CoverageDimension, evaluate_trace_policy, resolve_trace_policy
from sdai.version_entrypoint import main


FEATURE = "V010-TRACE"


def _git_executable() -> str:
    executable = shutil.which("git")
    if not executable:
        pytest.skip("git is required for the 0.10 release gate")
    return executable


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        [_git_executable(), *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _digest(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def _workspace(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "0.10 Release Ω workspace"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI 0.10 Release Gate")
    _git(root, "config", "user.email", "sdai-v010@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    _write(root / ".sdai" / "config.yaml", "project: v010-release-gate\n")

    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """# Requirements

- FR-001: Sign café scripts. AC-001 is the success scenario. ADR-001 and CONTRACT-001 define the design. TASK-001 implements it. TEST-001 verifies it. THREAT-001 and APPROVAL-001 complete release assurance.
- AC-001: Given a valid script, signing succeeds for FR-001 and TEST-001.
""",
    )
    _write(
        feature / "architecture.md",
        """# RFC-001: Signing architecture
RFC-001 references FR-001 and ADR-001.

# COMPONENT-001: Signing service
COMPONENT-001 references FR-001 and CONTRACT-001.
""",
    )
    _write(
        feature / "adr" / "ADR-001.md",
        """# ADR-001: Use external key custody
status: accepted

ADR-001 governs FR-001 and CONTRACT-001.
""",
    )
    _write(
        feature / "contracts" / "signing.yaml",
        """id: CONTRACT-001
status: approved
references: [FR-001, ADR-001]
""",
    )
    _write(
        feature / "tasks.md",
        """# Tasks

- [x] TASK-001: Implement signing for FR-001 using ADR-001 and CONTRACT-001; verified by TEST-001.
""",
    )
    _write(
        feature / "tests.md",
        """# Tests

- TEST-001: Verify FR-001 and AC-001 through TASK-001.
""",
    )
    _write(
        feature / "security" / "threats.yaml",
        """threat_id: THREAT-001
status: mitigated
references: [FR-001, TASK-001]
""",
    )
    _write(
        feature / "approvals" / "release.yaml",
        """approval_id: APPROVAL-001
status: approved
references: [FR-001, CONTRACT-001]
""",
    )
    _write(
        root / "src" / "signing" / "café.py",
        "# Trace: FR-001 AC-001 RFC-001 COMPONENT-001 ADR-001 CONTRACT-001 TASK-001 TEST-001 THREAT-001 APPROVAL-001\nSIGNED = True\n",
    )
    _write(
        root / "tests" / "test_signing.py",
        "# Trace: FR-001 AC-001 TASK-001 TEST-001\ndef test_signing():\n    assert True\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "0.10 trace release baseline")
    return root, _git(root, "rev-parse", "HEAD")


def _evidence(
    root: Path,
    commit: str,
    *,
    evidence_id: str,
    kind: EvidenceKind,
    provider: str = "codex",
    model: str = "release-gate-a",
) -> Path:
    source = root / "src" / "signing" / "café.py"
    test_source = root / "tests" / "test_signing.py"
    relative = f"specs/changes/{FEATURE}/evidence/{evidence_id}.json"
    record = TraceEvidence(
        evidence_id=evidence_id,
        kind=kind,
        status=EvidenceStatus.PASSED,
        subject="requirement:FR-001",
        git_commit=commit,
        bindings=(
            EvidenceBinding(EvidenceBindingKind.SOURCE, "src/signing/café.py", _digest(source)),
            EvidenceBinding(EvidenceBindingKind.TEST, "tests/test_signing.py", _digest(test_source)),
        ),
        provenance=(TraceProvenance(relative, 1),),
        producer=EvidenceProducer("tester", provider, model),
        result={"passed": True, "release_gate": "0.10"},
        command=("pytest", "-q", "tests/test_signing.py"),
        tool="pytest",
    )
    return _write(root / relative, record.to_json())


def _complete_evidence(root: Path, commit: str) -> None:
    _evidence(root, commit, evidence_id="EVIDENCE-TEST", kind=EvidenceKind.TEST)
    _evidence(root, commit, evidence_id="EVIDENCE-QUALITY", kind=EvidenceKind.QUALITY)
    _evidence(root, commit, evidence_id="EVIDENCE-SECURITY", kind=EvidenceKind.SECURITY)
    _evidence(root, commit, evidence_id="EVIDENCE-APPROVAL", kind=EvidenceKind.APPROVAL)


def _policy(path: Path, policy_id: str, requirements: int) -> Path:
    return _write(
        path,
        f"""apiVersion: sdai.trace-policy/v1
kind: TraceCoveragePolicy
metadata:
  id: {policy_id}
spec:
  risks:
    critical:
      requirements: {requirements}
""",
    )


def test_v010_complete_trace_journey_meets_critical_policy_and_all_node_families(
    tmp_path: Path,
) -> None:
    root, commit = _workspace(tmp_path)
    _complete_evidence(root, commit)

    build = build_feature_trace_graph(root, FEATURE, environ={})
    report = evaluate_trace_policy(root, FEATURE, "critical", environ={})
    node_types = {node.type for node in build.graph.nodes}
    dimensions = {item.dimension: item for item in report.dimensions}

    assert build.gaps == ()
    assert {
        TraceNodeType.REQUIREMENT,
        TraceNodeType.SCENARIO,
        TraceNodeType.RFC,
        TraceNodeType.ADR,
        TraceNodeType.COMPONENT,
        TraceNodeType.CONTRACT,
        TraceNodeType.THREAT,
        TraceNodeType.TASK,
        TraceNodeType.CODE,
        TraceNodeType.TEST,
        TraceNodeType.APPROVAL,
        TraceNodeType.EVIDENCE,
    } <= node_types
    assert report.passed is True
    assert all(item.threshold.required_percent == 100.0 for item in report.dimensions)
    assert all(item.actual_percent == 100.0 for item in report.dimensions)
    assert dimensions[CoverageDimension.SECURITY].numerator == 1
    assert dimensions[CoverageDimension.APPROVALS].numerator == 1


def test_v010_trace_cli_journey_is_read_only_and_export_is_exact(tmp_path: Path, capsys) -> None:
    root, commit = _workspace(tmp_path)
    _complete_evidence(root, commit)
    expected = build_feature_trace_graph(root, FEATURE, environ={}).graph.to_json()
    before = _snapshot(root)

    assert main(["trace", FEATURE, "--path", str(root)]) == 0
    assert "café.py" in capsys.readouterr().out
    assert main(["trace", "requirement", FEATURE, "FR-001", "--json", "--path", str(root)]) == 0
    requirement = json.loads(capsys.readouterr().out)
    assert requirement["covered"] is True
    assert main(["trace", "missing", FEATURE, "--json", "--path", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0
    assert main(["trace", "coverage", FEATURE, "--json", "--path", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["coverage_percent"] == 100.0
    assert main(["trace", "policy", FEATURE, "--risk", "critical", "--json", "--path", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    assert main(["trace", "export", FEATURE, "--format", "json", "--path", str(root)]) == 0
    exported = capsys.readouterr().out

    assert exported == expected
    assert _snapshot(root) == before


def test_v010_stale_evidence_never_satisfies_current_critical_coverage(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    _complete_evidence(root, commit)
    source = root / "src" / "signing" / "café.py"
    source.write_text(source.read_text(encoding="utf-8") + "# changed after proof Δ\n", encoding="utf-8", newline="\n")

    report = evaluate_trace_policy(root, FEATURE, "critical", environ={})
    dimensions = {item.dimension: item for item in report.dimensions}

    assert report.passed is False
    assert dimensions[CoverageDimension.REQUIREMENTS].actual_percent == 0.0
    assert dimensions[CoverageDimension.SECURITY].actual_percent == 0.0
    assert dimensions[CoverageDimension.APPROVALS].actual_percent == 0.0
    assert any(item.dimension is CoverageDimension.REQUIREMENTS for item in report.findings)


def test_v010_missing_link_journey_is_visible_and_blocks_missing_query(tmp_path: Path, capsys) -> None:
    root, commit = _workspace(tmp_path)
    _complete_evidence(root, commit)
    requirements = root / "specs" / "changes" / FEATURE / "requirements.md"
    requirements.write_text(
        requirements.read_text(encoding="utf-8") + "\n- FR-002: New brownfield requirement references ADR-MISSING.\n",
        encoding="utf-8",
        newline="\n",
    )

    code = main(["trace", "missing", FEATURE, "--json", "--path", str(root)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert any(item["target"] == "ADR-MISSING" for item in payload["gaps"])
    assert any(
        item["kind"] == "uncovered-requirement" and item["target"] == "FR-002"
        for item in payload["gaps"]
    )


def test_v010_provider_model_change_does_not_change_canonical_graph_truth(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    _complete_evidence(root, commit)
    first = build_feature_trace_graph(root, FEATURE, environ={}).graph

    _evidence(
        root,
        commit,
        evidence_id="EVIDENCE-QUALITY",
        kind=EvidenceKind.QUALITY,
        provider="another-provider",
        model="another-model",
    )
    second = build_feature_trace_graph(root, FEATURE, environ={}).graph

    assert first.sha256 == second.sha256
    assert first.to_json() == second.to_json()


def test_v010_organization_critical_minimum_cannot_be_weakened(tmp_path: Path) -> None:
    root, _ = _workspace(tmp_path)
    org = _policy(root / "external" / "org.yaml", "org-critical", 100)
    _policy(root / ".sdai" / "trace-policy.yaml", "repo-weaker", 10)
    user = _policy(root / "external" / "user.yaml", "user-weaker", 20)

    thresholds, _ = resolve_trace_policy(
        root,
        "critical",
        environ={
            "SDAI_ORG_TRACE_POLICY_PATH": str(org.resolve()),
            "SDAI_USER_TRACE_POLICY_PATH": str(user.resolve()),
        },
    )
    requirement = next(
        item for item in thresholds if item.dimension is CoverageDimension.REQUIREMENTS
    )

    assert requirement.required_percent == 100.0
    assert [item.layer.value for item in requirement.contributions] == [
        "builtin",
        "org",
        "repo",
        "user",
    ]
    assert {item["layer"] for item in requirement.as_dict()["enforced_by"]} >= {"builtin", "org"}


def test_v010_keeps_all_previous_release_compatibility_gates_enabled() -> None:
    tests = Path(__file__).resolve().parent
    for name in (
        "test_v06_release_compatibility.py",
        "test_v07_release_compatibility.py",
        "test_v08_release_compatibility.py",
        "test_v09_release_compatibility.py",
    ):
        path = tests / name
        assert path.is_file(), f"previous compatibility gate was removed: {name}"
        assert path.read_text(encoding="utf-8").strip(), f"previous compatibility gate is empty: {name}"
