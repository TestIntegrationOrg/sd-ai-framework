from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sdai.architecture_cli import main as architecture_main
from sdai.architecture_drift import ArchitectureFactKind, load_architecture_topology
from sdai.architecture_engine import evaluate_architecture_drift
from sdai.architecture_policy import (
    ARCHITECTURE_POLICY_API_VERSION,
    ORG_ARCHITECTURE_POLICY_ENV,
    load_effective_architecture_policy,
)
from sdai.architecture_trace import build_feature_trace_graph_with_architecture
from sdai.architecture_verify import verify_feature_with_architecture
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceNodeType, TraceProvenance, TraceRelation
from sdai.verification import VerificationSeverity
from sdai import version_entrypoint


FEATURE = "ARCH-GOV-222"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _git(root: Path, *args: str) -> str:
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
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _config(root: Path) -> None:
    _write(
        root / ".sdai" / "config.yaml",
        """version: 3
operating_mode: individual
policy:
  repository: .sdai/policy.yaml
  organization_env: SDAI_ORG_POLICY_PATH
  user_env: SDAI_USER_POLICY_PATH
""",
    )


def _feature_artifacts(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """# Requirements

- FR-001: Architecture drift must remain governed.
- AC-001: Given drift, deterministic policy is applied.
""",
    )
    _write(feature / "tasks.md", "# Tasks\n\n- [ ] TASK-001: Implement FR-001.\n")
    _write(feature / "tests.md", "# Tests\n\n- TEST-001: Verify FR-001 and AC-001.\n")
    return feature


def _init_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "architecture-governance@example.invalid")
    _git(root, "config", "user.name", "Architecture Governance Tests")
    _config(root)
    _feature_artifacts(root)
    return root


def _topology(root: Path, *, facts: str) -> str:
    approval = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    text = f"""apiVersion: sdai.architecture-topology/v1
kind: ApprovedArchitecture
metadata:
  id: governed-topology
  feature: {FEATURE}
  approvalEvidence: {approval}
spec:
  components:
    - id: api
      roots: [src/api]
      modulePrefixes: [example.api]
    - id: data
      roots: [src/data]
      modulePrefixes: [example.data]
  facts:
{facts}
"""
    _write(
        root / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml",
        text,
    )
    return approval


def _required_dependency() -> str:
    return """    - id: DEP-API-DATA
      kind: dependency
      mode: required
      source: api
      target: data
      attributes: {}
"""


def _approve(root: Path, evidence_relative: str) -> Path:
    topology = load_architecture_topology(root, FEATURE)
    record = TraceEvidence(
        evidence_id="ARCH-APPROVAL-222",
        kind=EvidenceKind.APPROVAL,
        status=EvidenceStatus.PASSED,
        subject=topology.subject,
        git_commit=_git(root, "rev-parse", "HEAD"),
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.ARTIFACT,
                topology.source,
                topology.file_sha256,
            ),
        ),
        provenance=(
            TraceProvenance(
                evidence_relative,
                1,
                detail="human architecture governance approval",
            ),
        ),
        producer=EvidenceProducer("architecture-approver", None, None),
        result={
            "architectureApproval": {
                "featureId": FEATURE,
                "topologyId": topology.topology_id,
                "topologySha256": topology.sha256,
            }
        },
        tool="sdai-architecture-approval",
    )
    return _write(root / evidence_relative, record.to_json())


def _approved_project(tmp_path: Path, *, facts: str, import_data: bool = False) -> Path:
    root = _init_project(tmp_path)
    approval = _topology(root, facts=facts)
    source = "import example.data\n" if import_data else "# api\n"
    _write(root / "src" / "api" / "service.py", source)
    _write(root / "src" / "data" / "__init__.py", "# data\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "add governed architecture")
    _approve(root, approval)
    return root


def _policy(required: bool, threshold: str) -> str:
    return f"""apiVersion: {ARCHITECTURE_POLICY_API_VERSION}
kind: ArchitectureDriftPolicy
required: {str(required).lower()}
defaultThreshold: {threshold}
kinds: {{}}
"""


def test_policy_layers_are_monotonic_and_lower_layers_cannot_weaken_org(tmp_path: Path) -> None:
    root = _init_project(tmp_path)
    org = tmp_path / "organization-architecture-policy.yaml"
    _write(org, _policy(True, "warning"))
    _write(root / ".sdai" / "architecture-drift-policy.yaml", _policy(False, "error"))

    effective = load_effective_architecture_policy(
        root,
        environ={ORG_ARCHITECTURE_POLICY_ENV: str(org.resolve())},
    )

    assert effective.required is True
    assert effective.default_threshold.value == "warning"
    assert [source.source for source in effective.sources] == [
        "core:sdai-0.17",
        "organization",
        "repository:.sdai/architecture-drift-policy.yaml",
    ]


def test_missing_topology_is_backward_compatible_until_policy_requires_it(tmp_path: Path, capsys) -> None:
    root = _init_project(tmp_path)

    assert architecture_main([FEATURE, "--json", "--path", str(root)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["topologyPresent"] is False
    assert first["decision"]["outcome"] == "allowed"

    _write(root / ".sdai" / "architecture-drift-policy.yaml", _policy(True, "error"))
    assert architecture_main([FEATURE, "--json", "--path", str(root)]) == 2
    second = json.loads(capsys.readouterr().out)
    assert second["decision"]["outcome"] == "blocked"
    assert second["decision"]["blockers"][0]["code"] == "ARCH-POLICY-TOPOLOGY-REQUIRED"


def test_required_dependency_drift_blocks_and_json_is_deterministic(tmp_path: Path) -> None:
    root = _approved_project(tmp_path, facts=_required_dependency(), import_data=False)

    first = evaluate_architecture_drift(root, FEATURE, environ={})
    second = evaluate_architecture_drift(root, FEATURE, environ={})

    assert first.to_json() == second.to_json()
    assert first.blocked is True
    assert first.report is not None
    finding = next(item for item in first.report.findings if item.code == "ARCH-DRIFT-REQUIRED-MISSING")
    assert finding.kind is ArchitectureFactKind.DEPENDENCY
    assert first.policy.blocks(finding) is True
    assert first.decision.report_sha256 == first.report.sha256


def test_warning_drift_is_nonblocking_by_core_and_repo_can_tighten_threshold(tmp_path: Path) -> None:
    root = _approved_project(tmp_path, facts="    []", import_data=True)

    baseline = evaluate_architecture_drift(root, FEATURE, environ={})
    assert baseline.report is not None
    unexpected = next(item for item in baseline.report.findings if item.code == "ARCH-DRIFT-UNEXPECTED-PRESENT")
    assert unexpected.kind is ArchitectureFactKind.DEPENDENCY
    assert unexpected.severity.value == "warning"
    assert baseline.blocked is False

    _write(root / ".sdai" / "architecture-drift-policy.yaml", _policy(False, "warning"))
    tightened = evaluate_architecture_drift(root, FEATURE, environ={})
    assert tightened.blocked is True
    assert tightened.policy.default_threshold.value == "warning"
    assert tightened.policy.sha256 != baseline.policy.sha256


def test_stale_or_mismatched_architecture_approval_is_policy_blocking(tmp_path: Path) -> None:
    root = _approved_project(tmp_path, facts=_required_dependency(), import_data=True)
    topology_path = root / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml"
    topology_path.write_text(
        topology_path.read_text(encoding="utf-8").replace("DEP-API-DATA", "DEP-API-DATA-CHANGED"),
        encoding="utf-8",
        newline="\n",
    )

    evaluation = evaluate_architecture_drift(root, FEATURE, environ={})

    assert evaluation.blocked is True
    assert evaluation.report is None
    assert evaluation.governance_error is not None
    assert "SDAI-ARCH-DRIFT-005" in evaluation.governance_error
    assert evaluation.decision.blockers[0].code == "ARCH-POLICY-APPROVAL-INVALID"


def test_trace_graph_projects_topology_facts_drift_and_approval_binding(tmp_path: Path) -> None:
    root = _approved_project(tmp_path, facts=_required_dependency(), import_data=False)

    result = build_feature_trace_graph_with_architecture(root, FEATURE, environ={})

    roles = {
        node.metadata.get("architecture_role")
        for node in result.graph.nodes
        if node.type is TraceNodeType.COMPONENT
    }
    assert {"topology", "component", "fact", "drift-finding"} <= roles
    topology = next(node for node in result.graph.nodes if node.metadata.get("architecture_role") == "topology")
    approval = next(node for node in result.graph.nodes if node.entity_id == "ARCH-APPROVAL-222")
    assert any(
        edge.relation is TraceRelation.EVIDENCED_BY
        and edge.source == topology.node_id
        and edge.target == approval.node_id
        for edge in result.graph.edges
    )
    assert not any(
        gap.kind == "missing-evidence-subject"
        and gap.target.startswith(f"architecture-topology:{FEATURE}:")
        for gap in result.gaps
    )


def test_verify_projects_architecture_drift_into_existing_verification_model(tmp_path: Path) -> None:
    root = _approved_project(tmp_path, facts=_required_dependency(), import_data=False)

    report = verify_feature_with_architecture(root, FEATURE, risk="standard", environ={})

    architecture = [item for item in report.findings if item.code == "SDAI_VERIFY_ARCH_ARCH_DRIFT_REQUIRED_MISSING"]
    assert len(architecture) == 1
    assert architecture[0].severity is VerificationSeverity.BLOCKING
    assert architecture[0].metadata["architecture_kind"] == "dependency"
    assert architecture[0].metadata["architecture_report_sha256"].startswith("sha256:")


def test_versioned_entrypoint_routes_only_nested_architecture_drift(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def architecture(argv: list[str]) -> int:
        seen.append(("architecture", argv))
        return 7

    def lifecycle(argv: list[str]) -> int:
        seen.append(("lifecycle", argv))
        return 9

    monkeypatch.setattr(version_entrypoint, "architecture_main", architecture)
    monkeypatch.setattr(version_entrypoint, "lifecycle_main", lifecycle)

    assert version_entrypoint.main(["architecture", "drift", FEATURE]) == 7
    assert version_entrypoint.main(["architecture", FEATURE]) == 9
    assert seen == [
        ("architecture", [FEATURE]),
        ("lifecycle", ["architecture", FEATURE]),
    ]
