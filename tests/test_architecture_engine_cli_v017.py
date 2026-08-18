from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sdai.architecture_cli import main as architecture_main
from sdai.architecture_drift import ArchitectureDriftError, ArchitectureDriftSeverity, load_architecture_topology
from sdai.architecture_engine import (
    ARCHITECTURE_BLOCKED_EXIT_CODE,
    ArchitecturePolicyLayer,
    check_architecture,
    load_project_architecture_policy,
    resolve_architecture_policy,
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

FEATURE = "ARCH-ENGINE-222"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="strict", check=False, shell=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _topology(root: Path, *, forbidden_dependency: bool = False) -> None:
    fact = ""
    if forbidden_dependency:
        fact = """    - id: NO-API-DATA
      kind: dependency
      mode: forbidden
      source: api
      target: data
      attributes: {}
"""
    facts = fact if fact else "    []\n"
    approval = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    _write(root / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml", f"""apiVersion: sdai.architecture-topology/v1
kind: ApprovedArchitecture
metadata:
  id: engine-topology
  feature: {FEATURE}
  approvalEvidence: {approval}
spec:
  components:
    - id: api
      roots: [src/api]
      modulePrefixes: [acme.api]
    - id: data
      roots: [src/data]
      modulePrefixes: [acme.data]
  facts:
{facts}""")


def _approve(root: Path) -> None:
    topology = load_architecture_topology(root, FEATURE)
    relative = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    evidence = TraceEvidence(
        evidence_id="ARCH-APPROVAL-222",
        kind=EvidenceKind.APPROVAL,
        status=EvidenceStatus.PASSED,
        subject=topology.subject,
        git_commit=_git(root, "rev-parse", "HEAD"),
        bindings=(EvidenceBinding(EvidenceBindingKind.ARTIFACT, topology.source, topology.file_sha256),),
        provenance=(TraceProvenance(relative, 1, detail="architecture engine approval"),),
        producer=EvidenceProducer("architecture-approver", None, None),
        result={"architectureApproval": {
            "featureId": FEATURE,
            "topologyId": topology.topology_id,
            "topologySha256": topology.sha256,
        }},
        tool="sdai-architecture-approval",
    )
    _write(root / relative, evidence.to_json())


def _project(tmp_path: Path, *, forbidden_dependency: bool = False) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "architecture-engine@example.invalid")
    _git(root, "config", "user.name", "Architecture Engine Tests")
    _topology(root, forbidden_dependency=forbidden_dependency)
    _write(root / "src" / "api" / "placeholder.txt", "api\n")
    _write(root / "src" / "data" / "placeholder.txt", "data\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "add architecture topology")
    _approve(root)
    return root


def test_clean_architecture_result_is_deterministic_and_core_policy_is_present(tmp_path: Path) -> None:
    root = _project(tmp_path)
    first = check_architecture(root, FEATURE)
    second = check_architecture(root, FEATURE)

    assert first.status == "passed"
    assert first.blocking_codes == ()
    assert first.to_json() == second.to_json()
    payload = json.loads(first.to_json())
    assert payload["apiVersion"] == "sdai.architecture-check/v1"
    assert payload["policy"]["blockSeverities"] == ["error"]
    assert payload["sha256"] == first.sha256


def test_forbidden_dependency_is_blocked_and_cli_check_uses_stable_exit(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path, forbidden_dependency=True)
    _write(root / "src" / "api" / "service.py", "from acme.data import repository\n")

    result = check_architecture(root, FEATURE)
    assert result.blocked
    assert "ARCH-DRIFT-FORBIDDEN-PRESENT" in result.blocking_codes

    inspect_exit = architecture_main(["inspect", FEATURE, "--path", str(root), "--json"])
    inspect_payload = json.loads(capsys.readouterr().out)
    assert inspect_exit == 0
    assert inspect_payload["status"] == "blocked"

    check_exit = architecture_main(["check", FEATURE, "--path", str(root), "--json"])
    check_payload = json.loads(capsys.readouterr().out)
    assert check_exit == ARCHITECTURE_BLOCKED_EXIT_CODE
    assert check_payload["blockingCodes"] == list(result.blocking_codes)


def test_project_policy_can_only_tighten_and_unknown_weakening_fields_fail_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root / ".sdai" / "architecture-policy.yaml", """apiVersion: sdai.architecture-policy/v1
kind: ArchitecturePolicy
blockSeverities: [warning]
blockCodes: [ARCH-CUSTOM-EXTRA]
""")
    layer = load_project_architecture_policy(root)
    assert layer is not None
    effective = resolve_architecture_policy(project=layer)
    assert {item.value for item in effective.block_severities} == {"error", "warning"}
    assert effective.block_codes == ("ARCH-CUSTOM-EXTRA",)

    _write(root / ".sdai" / "architecture-policy.yaml", """apiVersion: sdai.architecture-policy/v1
kind: ArchitecturePolicy
allowCodes: [ARCH-DRIFT-FORBIDDEN-PRESENT]
""")
    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-POLICY-004.*weakening"):
        load_project_architecture_policy(root)


def test_organization_and_project_layers_compose_additively() -> None:
    organization = ArchitecturePolicyLayer(
        "organization",
        block_severities=(ArchitectureDriftSeverity.WARNING,),
        block_codes=("ARCH-ORG-REQUIRED",),
    )
    project = ArchitecturePolicyLayer("project", block_codes=("ARCH-PROJECT-REQUIRED",))
    effective = resolve_architecture_policy(organization, project)

    assert [layer.layer_id for layer in effective.layers] == ["core", "organization", "project"]
    assert {item.value for item in effective.block_severities} == {"error", "warning"}
    assert effective.block_codes == ("ARCH-ORG-REQUIRED", "ARCH-PROJECT-REQUIRED")


def test_stale_topology_approval_remains_fail_closed_through_unified_engine(tmp_path: Path) -> None:
    root = _project(tmp_path)
    topology_path = root / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml"
    topology_path.write_text(topology_path.read_text(encoding="utf-8") + "\n# changed after approval\n", encoding="utf-8")

    with pytest.raises(ArchitectureDriftError):
        check_architecture(root, FEATURE)
