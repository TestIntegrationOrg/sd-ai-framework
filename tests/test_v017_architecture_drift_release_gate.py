from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sdai import version_entrypoint
from sdai.architecture_deployment_observer import deployment_subject_id, load_deployment_sources
from sdai.architecture_drift import ArchitectureDriftError, load_architecture_topology
from sdai.architecture_engine import evaluate_architecture_drift
from sdai.contract_trace import build_contract_trace_index
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance


FEATURE = "RELEASE-017"


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


def _fact(
    fact_id: str,
    kind: str,
    mode: str,
    source: str,
    target: str,
    attributes: dict[str, object],
) -> str:
    payload = json.dumps(attributes, sort_keys=True, separators=(",", ":"))
    return f"""    - id: {fact_id}
      kind: {kind}
      mode: {mode}
      source: {source}
      target: {target}
      attributes: {payload}
"""


def _feature_artifacts(root: Path, *, legacy: bool = False) -> Path:
    feature = root / "specs" / (FEATURE if legacy else f"changes/{FEATURE}")
    _write(
        feature / "requirements.md",
        """# Requirements

- FR-017: Architecture drift must be deterministic, governed, and provenance-rich.
- AC-017: Material drift blocks delivery while approved non-material drift remains non-blocking.
""",
    )
    _write(feature / "tasks.md", "# Tasks\n\n- [ ] TASK-017: Implement and verify FR-017.\n")
    _write(feature / "tests.md", "# Tests\n\n- TEST-017: Verify FR-017 and AC-017.\n")
    return feature


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


def _contracts(root: Path) -> tuple[str, str]:
    _write(
        root / ".sdai" / "contracts.yaml",
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: public-api
    kind: openapi
    path: contracts/public-api.yaml
""",
    )
    _write(
        root / "contracts" / "public-api.yaml",
        """openapi: 3.1.0
info:
  title: Release API
  version: 1.0.0
paths:
  /pets:
    get:
      operationId: listPets
      responses:
        '200':
          description: ok
""",
    )
    index = build_contract_trace_index(root)
    source = index.sources["public-api"]
    symbol = index.symbols[("public-api", "/paths/~1pets/get")]
    source_sha = source.metadata["source_sha256"]
    assert isinstance(source_sha, str)
    return source_sha, symbol.symbol_sha256


def _deployment_sources(root: Path) -> tuple[str, str]:
    _write(
        root / ".sdai" / "deployments.yaml",
        """apiVersion: sdai.deployment-sources/v1
kind: DeploymentSources
sources:
  - id: compose-prod
    kind: compose
    path: deploy/compose.yaml
    environment: prod
""",
    )
    _write(
        root / "deploy" / "compose.yaml",
        """services:
  api:
    x-sdai-component: api
    image: example.invalid/api:1
  data:
    x-sdai-component: data
    image: example.invalid/data:1
""",
    )
    namespace = "compose:compose-prod"
    return (
        deployment_subject_id("compose-prod", "compose", namespace, "service", "api"),
        deployment_subject_id("compose-prod", "compose", namespace, "service", "data"),
    )


def _topology(root: Path, *, source_sha: str, symbol_sha: str, api_workload: str, data_workload: str) -> str:
    approval = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    facts = "".join(
        (
            _fact("DEP-API-DATA", "dependency", "required", "api", "data", {}),
            _fact(
                "COMM-API-DATA",
                "communication",
                "required",
                "api",
                "data",
                {
                    "direction": "outbound",
                    "protocol": "http",
                    "method": "GET",
                    "endpoint": "/health",
                    "host": "data.internal",
                    "transport": "https",
                },
            ),
            _fact(
                "CONTRACT-PUBLIC",
                "contract",
                "required",
                "api",
                "contract:public-api",
                {
                    "sourceId": "public-api",
                    "sourceSha256": source_sha,
                    "address": "/paths/~1pets/get",
                    "symbolSha256": symbol_sha,
                },
            ),
            _fact(
                "DATA-CUSTOMERS-OWNER",
                "data-ownership",
                "required",
                "data",
                "data:resource:customers",
                {"resource": "customers", "resourceType": "table"},
            ),
            _fact(
                "DATA-CUSTOMERS-ADMIN",
                "data-access",
                "required",
                "data",
                "data:resource:customers",
                {"resource": "customers", "resourceType": "table", "access": "admin"},
            ),
            _fact("ZONE-API", "trust-boundary", "allowed", "api", "zone:public", {"role": "zone-membership"}),
            _fact("ZONE-DATA", "trust-boundary", "allowed", "data", "zone:internal", {"role": "zone-membership"}),
            _fact(
                "ZONE-API-WORKLOAD",
                "trust-boundary",
                "allowed",
                api_workload,
                "zone:public",
                {"role": "zone-membership"},
            ),
            _fact(
                "ZONE-DATA-WORKLOAD",
                "trust-boundary",
                "allowed",
                data_workload,
                "zone:internal",
                {"role": "zone-membership"},
            ),
            _fact(
                "BOUNDARY-HTTP",
                "trust-boundary",
                "allowed",
                "zone:public",
                "zone:internal",
                {
                    "role": "boundary-rule",
                    "evidenceKind": "communication",
                    "direction": "outbound",
                    "protocol": "http",
                },
            ),
        )
    )
    _write(
        root / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml",
        f"""apiVersion: sdai.architecture-topology/v1
kind: ApprovedArchitecture
metadata:
  id: release-017-topology
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
{facts}""",
    )
    return approval


def _approve(root: Path, evidence_relative: str) -> Path:
    topology = load_architecture_topology(root, FEATURE)
    record = TraceEvidence(
        evidence_id="ARCHITECTURE-APPROVAL-017",
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
                detail="human architecture approval for the 0.17 release topology",
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


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "release-017@example.invalid")
    _git(root, "config", "user.name", "SD-AI 0.17 Release Gate")
    _config(root)
    _feature_artifacts(root)
    source_sha, symbol_sha = _contracts(root)
    api_workload, data_workload = _deployment_sources(root)
    approval = _topology(
        root,
        source_sha=source_sha,
        symbol_sha=symbol_sha,
        api_workload=api_workload,
        data_workload=data_workload,
    )
    _write(
        root / "src" / "api" / "client.py",
        """from acme.data import repo


def health():
    return requests.get('https://data.internal/health')
""",
    )
    _write(root / "src" / "data" / "repo.py", "# data component\n")
    _write(root / "src" / "data" / "schema.sql", "CREATE TABLE customers (id bigint primary key);\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "prepare 0.17 architecture release fixture")
    _approve(root, approval)
    return root


def _json_stdout(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_v017_integrated_architecture_drift_release_journey(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)

    first = evaluate_architecture_drift(root, FEATURE, environ={})
    second = evaluate_architecture_drift(root, FEATURE, environ={})
    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert first.report is not None
    assert first.blocked is False
    assert first.decision.outcome == "allowed"
    assert all(finding.severity.value == "warning" for finding in first.report.findings)
    assert any(
        finding.kind.value == "deployment" and finding.code == "ARCH-DRIFT-UNEXPECTED-PRESENT"
        for finding in first.report.findings
    )
    observer_ids = {item.observer_id for item in first.report.observations}
    assert {
        "repository-dependencies",
        "repository-communications",
        "repository-data",
        "repository-deployments",
        "trust-boundary-security",
    } <= observer_ids

    assert version_entrypoint.main(
        ["architecture", "drift", FEATURE, "--json", "--path", str(root)]
    ) == 0
    baseline_cli = _json_stdout(capsys)
    assert baseline_cli["apiVersion"] == "sdai.architecture-drift-evaluation/v1"
    assert baseline_cli["sha256"] == first.sha256
    assert baseline_cli["decision"]["outcome"] == "allowed"

    assert version_entrypoint.main(
        ["trace", "export", FEATURE, "--format", "json", "--path", str(root)]
    ) == 0
    trace = _json_stdout(capsys)
    architecture_roles = {
        node.get("metadata", {}).get("architecture_role")
        for node in trace["nodes"]
    }
    assert {"topology", "component", "fact"} <= architecture_roles
    assert any(
        edge["relation"] == "evidenced-by"
        and edge.get("metadata", {}).get("architecture_relation") == "approved-by"
        for edge in trace["edges"]
    )

    _write(root / "src" / "api" / "client.py", "# required edge intentionally removed\n")

    blocked_first = evaluate_architecture_drift(root, FEATURE, environ={})
    blocked_second = evaluate_architecture_drift(root, FEATURE, environ={})
    assert blocked_first.to_json() == blocked_second.to_json()
    assert blocked_first.blocked is True
    assert blocked_first.report is not None
    required_missing = [
        item
        for item in blocked_first.report.findings
        if item.code == "ARCH-DRIFT-REQUIRED-MISSING"
    ]
    assert {item.kind.value for item in required_missing} >= {"dependency", "communication"}
    assert all(item.severity.value == "error" for item in required_missing)

    assert version_entrypoint.main(
        ["architecture", "drift", FEATURE, "--json", "--path", str(root)]
    ) == 2
    blocked_cli = _json_stdout(capsys)
    assert blocked_cli["sha256"] == blocked_first.sha256
    assert blocked_cli["decision"]["outcome"] == "blocked"

    assert version_entrypoint.main(
        ["trace", "export", FEATURE, "--format", "json", "--path", str(root)]
    ) == 0
    drift_trace = _json_stdout(capsys)
    assert any(
        node.get("metadata", {}).get("architecture_role") == "drift-finding"
        for node in drift_trace["nodes"]
    )

    assert version_entrypoint.main(
        ["verify", FEATURE, "--json", "--path", str(root)]
    ) == 2
    verification = _json_stdout(capsys)
    architecture_findings = [
        item
        for item in verification["findings"]
        if item["code"] == "SDAI_VERIFY_ARCH_ARCH_DRIFT_REQUIRED_MISSING"
    ]
    assert {item["metadata"]["architecture_kind"] for item in architecture_findings} >= {
        "dependency",
        "communication",
    }
    assert all(item["severity"] == "blocking" for item in architecture_findings)


def test_v017_upgrade_compatibility_and_fail_closed_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _config(legacy)
    _feature_artifacts(legacy, legacy=True)

    assert version_entrypoint.main(
        ["architecture", "drift", FEATURE, "--json", "--path", str(legacy)]
    ) == 0
    compatible = _json_stdout(capsys)
    assert compatible["topologyPresent"] is False
    assert compatible["decision"]["outcome"] == "allowed"

    _write(
        legacy / ".sdai" / "architecture-drift-policy.yaml",
        """apiVersion: sdai.architecture-drift-policy/v1
kind: ArchitectureDriftPolicy
required: true
defaultThreshold: error
kinds: {}
""",
    )
    assert version_entrypoint.main(
        ["architecture", "drift", FEATURE, "--json", "--path", str(legacy)]
    ) == 2
    governed = _json_stdout(capsys)
    assert governed["decision"]["blockers"][0]["code"] == "ARCH-POLICY-TOPOLOGY-REQUIRED"

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    _write(
        unsafe / ".sdai" / "deployments.yaml",
        """apiVersion: sdai.deployment-sources/v1
kind: DeploymentSources
sources:
  - id: unsafe
    kind: compose
    path: ../outside.yaml
    environment: prod
""",
    )
    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DEPLOY-002"):
        load_deployment_sources(unsafe)

    _write(
        unsafe / ".sdai" / "deployments.yaml",
        """apiVersion: sdai.deployment-sources/v1
kind: DeploymentSources
sources:
  - id: unknown
    kind: cloud-live
    path: deploy/live.yaml
    environment: prod
""",
    )
    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DEPLOY-002.*unsupported kind"):
        load_deployment_sources(unsafe)

    malformed = tmp_path / "malformed"
    malformed.mkdir()
    _config(malformed)
    _feature_artifacts(malformed)
    _write(
        malformed / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml",
        """apiVersion: sdai.architecture-topology/v999
kind: ApprovedArchitecture
metadata: {}
spec: {}
""",
    )
    assert version_entrypoint.main(
        ["architecture", "drift", FEATURE, "--json", "--path", str(malformed)]
    ) == 1
    assert "SDAI-ARCH-DRIFT" in capsys.readouterr().err

    stale = _project(tmp_path / "stale")
    topology = stale / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml"
    topology.write_text(
        topology.read_text(encoding="utf-8").replace("DEP-API-DATA", "DEP-API-DATA-CHANGED"),
        encoding="utf-8",
        newline="\n",
    )
    assert version_entrypoint.main(
        ["architecture", "drift", FEATURE, "--json", "--path", str(stale)]
    ) == 2
    stale_json = _json_stdout(capsys)
    assert stale_json["decision"]["blockers"][0]["code"] == "ARCH-POLICY-APPROVAL-INVALID"


def test_v017_release_gate_keeps_historical_gates_and_full_matrix_enabled() -> None:
    root = Path(__file__).resolve().parents[1]
    required = {
        "tests/test_v06_release_compatibility.py",
        "tests/test_v07_release_compatibility.py",
        "tests/test_v08_release_compatibility.py",
        "tests/test_v09_release_compatibility.py",
        "tests/test_v010_release_compatibility.py",
        "tests/test_v011_release_evidence.py",
        "tests/test_pack_signed_lifecycle_gate_v012.py",
        "tests/test_integration_sdk_release_gate_v013.py",
        "tests/test_workflow_engine2_release_gate_v014.py",
        "tests/test_multi_repo_authority_hardening_v015.py",
        "tests/test_v015_release_readiness.py",
        "tests/test_v016_contract_engineering_release_gate.py",
    }
    assert all((root / path).is_file() for path in required)

    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert 'python-version: ["3.11", "3.12"]' in workflow
    assert "pytest -q" in workflow
    assert "pytest -q tests/" not in workflow

    capability_doc = (root / "docs" / "ARCHITECTURE-DRIFT.md").read_text(encoding="utf-8")
    release_doc = (root / "docs" / "releases" / "0.17-release-evidence.md").read_text(encoding="utf-8")
    assert "sdai architecture drift" in capability_doc
    assert "sdai.architecture-drift-evaluation/v1" in capability_doc
    assert "projects without approved topology" in capability_doc
    assert "0.6–0.16" in release_doc
    assert "merged `main`" in release_doc
