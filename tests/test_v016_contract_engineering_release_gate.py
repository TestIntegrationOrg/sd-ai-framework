from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess

import pytest

from sdai.constitution import init_constitution, load_constitution
from sdai.contract_adapters import default_contract_registry
from sdai.contract_cli import main as contract_main
from sdai.contract_gate import evaluate_contract_gate
from sdai.contract_policy import (
    ContractChangeClass,
    ContractCriticality,
    ContractPolicyOutcome,
    load_effective_contract_policy,
)
from sdai.contract_trace import build_contract_trace_index
from sdai.contracts import (
    CompatibilityDirection,
    ContractError,
    check_contract,
    diff_contracts,
    discover_contracts,
    load_explicit_snapshot,
)
from sdai.trace_builder import build_feature_trace_graph
from sdai.trace_cli import main as trace_main
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance, TraceRelation


FEATURE = "RELEASE-016"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        shell=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _sha(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def _clear_policy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SDAI_OPERATING_MODE",
        "SDAI_ORG_POLICY_PATH",
        "SDAI_USER_POLICY_PATH",
        "SDAI_ORG_CONTRACT_POLICY_PATH",
        "SDAI_USER_CONTRACT_POLICY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _manifest(root: Path, *, public_api_path: str = "contracts/public-api.yaml") -> None:
    _write(
        root / ".sdai" / "contracts.yaml",
        f"""apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: events
    kind: asyncapi
    path: contracts/events.yaml
  - id: profile-schema
    kind: json-schema
    path: contracts/profile.schema.json
  - id: public-api
    kind: openapi
    path: {public_api_path}
  - id: users-proto
    kind: protobuf
    path: contracts/users.proto
""",
    )


def _project(root: Path) -> Path:
    root.mkdir()
    _write(root / ".sdai" / "config.yaml", "{}\n")
    init_constitution(root)
    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """# Requirements

- FR-016: Contract changes must be deterministic, governed, traceable, and migration-safe.
""",
    )
    _write(
        feature / "tasks.md",
        """# Tasks

- [ ] TASK-016: Implement and verify FR-016.
""",
    )
    _write(
        feature / "tests.md",
        """# Tests

- TEST-016: Verify FR-016 across all supported contract formats.
""",
    )
    _write(
        feature / "adr" / "ADR-016.md",
        """# ADR-016: Govern breaking contract changes
status: accepted

ADR-016 records the deterministic compatibility and rollout decision for FR-016.
""",
    )
    _write(
        feature / "approvals" / "architecture.yaml",
        """approval_id: APPROVAL-016
status: approved
references: [ADR-016, FR-016]
""",
    )
    _write(
        feature / "migration-plan.md",
        """# Migration plan

Consumers receive a compatibility notice, dual-read window, rollback path, and completion criteria.
""",
    )
    _write(
        root / "tests" / "contract-release-check.txt",
        "FR-016 TEST-016 deterministic contract release proof\n",
    )

    _manifest(root)
    _write(
        root / "contracts" / "public-api.yaml",
        """openapi: 3.1.0
info:
  title: Public API
  version: 1.0.0
paths:
  /pets:
    get:
      operationId: listPets
      responses:
        '200':
          description: ok
        '404':
          description: not found
""",
    )
    _write(
        root / "contracts" / "candidates" / "public-api.yaml",
        """openapi: 3.1.0
info:
  title: Public API
  version: 2.0.0
paths:
  /pets:
    get:
      operationId: listPets
      responses:
        '200':
          description: ok
""",
    )
    _write(
        root / "contracts" / "events.yaml",
        """asyncapi: 2.6.0
info:
  title: Events
  version: 1.0.0
channels:
  users/signed:
    publish:
      message:
        name: UserSigned
        payload:
          type: object
          properties:
            userId:
              type: string
""",
    )
    _write(
        root / "contracts" / "candidates" / "events.yaml",
        """asyncapi: 2.6.0
info:
  title: Events
  version: 2.0.0
channels:
  users/signed:
    publish:
      message:
        name: UserSigned
        payload:
          type: object
          properties:
            userId:
              type: integer
""",
    )
    _write(
        root / "contracts" / "profile.schema.json",
        '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"string"}\n',
    )
    _write(
        root / "contracts" / "candidates" / "profile.schema.json",
        '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"integer"}\n',
    )
    _write(
        root / "contracts" / "users.proto",
        """syntax = "proto3";
package release;

message User {
  string id = 1;
}
""",
    )
    _write(
        root / "contracts" / "candidates" / "users.proto",
        """syntax = "proto3";
package release;

message User {
  int32 id = 1;
}
""",
    )

    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "sdai-release-tests@example.invalid")
    _git(root, "config", "user.name", "SD-AI 0.16 Release Gate")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "prepare 0.16 contract release fixture")
    return root


def _candidate_path(source_id: str) -> str:
    return {
        "public-api": "contracts/candidates/public-api.yaml",
        "events": "contracts/candidates/events.yaml",
        "profile-schema": "contracts/candidates/profile.schema.json",
        "users-proto": "contracts/candidates/users.proto",
    }[source_id]


def _contract_claim(
    *,
    evidence_type: str,
    diff,
    policy_sha256: str,
    constitution_sha256: str,
) -> dict[str, object]:
    return {
        "contractPolicy": {
            "evidenceType": evidence_type,
            "baselineSha256": diff.before.sha256,
            "candidateSha256": diff.after.sha256,
            "diffSha256": diff.sha256,
            "policySha256": policy_sha256,
            "constitutionSha256": constitution_sha256,
        }
    }


def _governance_evidence(root: Path, diff) -> tuple[Path, Path, Path]:
    feature = root / "specs" / "changes" / FEATURE
    commit = _git(root, "rev-parse", "HEAD")
    policy = load_effective_contract_policy(root, environ={})
    constitution = load_constitution(root)
    constitution_sha256 = "sha256:" + constitution.sha256

    architecture_path = feature / "evidence" / "architecture-approval.json"
    architecture = TraceEvidence(
        evidence_id="ARCHITECTURE-APPROVAL-016",
        kind=EvidenceKind.APPROVAL,
        status=EvidenceStatus.PASSED,
        subject="contract:source:public-api",
        git_commit=commit,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.ARTIFACT,
                f"specs/changes/{FEATURE}/approvals/architecture.yaml",
                _sha(feature / "approvals" / "architecture.yaml"),
            ),
        ),
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/evidence/architecture-approval.json",
                1,
                detail="human architecture approval for critical contract change",
            ),
        ),
        producer=EvidenceProducer("architecture-approver", None, None),
        result=_contract_claim(
            evidence_type="architecture-approval",
            diff=diff,
            policy_sha256=policy.sha256,
            constitution_sha256=constitution_sha256,
        ),
        tool="sdai-human-approval",
    )
    _write(architecture_path, architecture.to_json())

    migration_path = feature / "evidence" / "migration-plan.json"
    migration = TraceEvidence(
        evidence_id="MIGRATION-016",
        kind=EvidenceKind.REVIEW,
        status=EvidenceStatus.PASSED,
        subject="contract:source:public-api",
        git_commit=commit,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.ARTIFACT,
                f"specs/changes/{FEATURE}/migration-plan.md",
                _sha(feature / "migration-plan.md"),
            ),
        ),
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/evidence/migration-plan.json",
                1,
                detail="reviewed migration plan for critical contract change",
            ),
        ),
        producer=EvidenceProducer("migration-reviewer", None, None),
        result=_contract_claim(
            evidence_type="migration-plan",
            diff=diff,
            policy_sha256=policy.sha256,
            constitution_sha256=constitution_sha256,
        ),
        tool="sdai-migration-review",
    )
    _write(migration_path, migration.to_json())

    requirement_path = feature / "evidence" / "requirement-test.json"
    requirement = TraceEvidence(
        evidence_id="REQUIREMENT-TEST-016",
        kind=EvidenceKind.TEST,
        status=EvidenceStatus.PASSED,
        subject="requirement:FR-016",
        git_commit=commit,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.TEST,
                "tests/contract-release-check.txt",
                _sha(root / "tests" / "contract-release-check.txt"),
            ),
        ),
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/evidence/requirement-test.json",
                1,
                detail="deterministic requirement test proof",
            ),
        ),
        producer=EvidenceProducer("tester", None, None),
        result={"passed": True},
        command=("python", "-m", "pytest", "-q"),
        tool="pytest",
    )
    _write(requirement_path, requirement.to_json())
    return architecture_path, migration_path, requirement_path


def _trace_promotion(root: Path, decision) -> None:
    feature = root / "specs" / "changes" / FEATURE
    _manifest(root, public_api_path="contracts/candidates/public-api.yaml")
    index = build_contract_trace_index(root)
    source = index.sources["public-api"]
    source_sha256 = source.metadata["source_sha256"]
    assert source_sha256 == decision.candidate_sha256
    symbol = index.symbols[("public-api", "/paths/~1pets/get")]

    decision_path = feature / "evidence" / "contract-policy-decision.json"
    _write(decision_path, decision.to_json())
    links = []
    for target in (
        "requirement:FR-016",
        "task:TASK-016",
        "test:TEST-016",
        "adr:ADR-016",
        "approval:APPROVAL-016",
        "evidence:MIGRATION-016",
    ):
        links.append(
            {
                "contract": {
                    "sourceId": "public-api",
                    "address": symbol.address,
                },
                "target": target,
                "sourceSha256": source_sha256,
                "symbolSha256": symbol.symbol_sha256,
                "decision": {
                    "path": decision_path.relative_to(root).as_posix(),
                    "sha256": decision.sha256,
                },
            }
        )
    _write(
        feature / "contract-trace.yaml",
        json.dumps(
            {
                "apiVersion": "sdai.contract-trace/v1",
                "kind": "ContractTrace",
                "links": links,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
    )


def test_v016_integrated_contract_engineering_release_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_policy_environment(monkeypatch)
    root = _project(tmp_path / "project")

    inspection = discover_contracts(root)
    assert [item.source.source_id for item in inspection.sources] == [
        "events",
        "profile-schema",
        "public-api",
        "users-proto",
    ]
    assert inspection.to_json() == discover_contracts(root).to_json()
    registry = default_contract_registry(inspection.sources)

    diffs = {}
    for snapshot in inspection.sources:
        check = check_contract(snapshot, registry)
        assert check.valid, check.to_json()
        assert check.to_json() == check_contract(snapshot, registry).to_json()
        candidate = load_explicit_snapshot(
            root,
            source_id=snapshot.source.source_id,
            kind=snapshot.source.kind,
            path=_candidate_path(snapshot.source.source_id),
        )
        candidate_check = check_contract(candidate, registry)
        assert candidate_check.valid, candidate_check.to_json()
        diff = diff_contracts(snapshot, candidate, registry, CompatibilityDirection.FULL)
        repeated = diff_contracts(snapshot, candidate, registry, CompatibilityDirection.FULL)
        assert diff.to_json() == repeated.to_json()
        assert not diff.compatible
        assert diff.findings
        diffs[snapshot.source.source_id] = diff

    public_diff = diffs["public-api"]
    blocked = evaluate_contract_gate(
        root,
        public_diff,
        criticality=ContractCriticality.CRITICAL,
        evidence_paths=(),
        environ={},
    )
    assert blocked.change_class is ContractChangeClass.BREAKING
    assert blocked.outcome is ContractPolicyOutcome.BLOCKED
    assert set(item.value for item in blocked.required_evidence) == {
        "architecture-approval",
        "migration-plan",
    }

    architecture_path, migration_path, _ = _governance_evidence(root, public_diff)
    allowed = evaluate_contract_gate(
        root,
        public_diff,
        criticality=ContractCriticality.CRITICAL,
        evidence_paths=(architecture_path, migration_path),
        environ={},
    )
    repeated_allowed = evaluate_contract_gate(
        root,
        public_diff,
        criticality=ContractCriticality.CRITICAL,
        evidence_paths=(architecture_path, migration_path),
        environ={},
    )
    assert allowed.outcome is ContractPolicyOutcome.ALLOWED
    assert allowed.to_json() == repeated_allowed.to_json()
    assert {item.evidence_type.value for item in allowed.evidence if item.accepted} == {
        "architecture-approval",
        "migration-plan",
    }

    assert contract_main(["inspect", "--path", str(root), "--json"]) == 0
    inspect_json = json.loads(capsys.readouterr().out)
    assert inspect_json["apiVersion"] == "sdai.contract-result/v1"
    assert contract_main(["check", "public-api", "--path", str(root), "--json"]) == 0
    check_json = json.loads(capsys.readouterr().out)
    assert check_json["valid"] is True
    assert contract_main(
        [
            "diff",
            "public-api",
            "--against",
            "contracts/candidates/public-api.yaml",
            "--direction",
            "full",
            "--path",
            str(root),
            "--json",
        ]
    ) == 1
    diff_json = json.loads(capsys.readouterr().out)
    assert diff_json["compatible"] is False
    assert contract_main(
        [
            "gate",
            "public-api",
            "--against",
            "contracts/candidates/public-api.yaml",
            "--direction",
            "full",
            "--criticality",
            "critical",
            "--evidence",
            architecture_path.relative_to(root).as_posix(),
            "--evidence",
            migration_path.relative_to(root).as_posix(),
            "--path",
            str(root),
            "--json",
        ]
    ) == 0
    gate_json = json.loads(capsys.readouterr().out)
    assert gate_json["apiVersion"] == "sdai.contract-policy-decision/v1"
    assert gate_json["sha256"] == allowed.sha256
    assert gate_json["allowed"] is True

    _trace_promotion(root, allowed)
    first_trace = build_feature_trace_graph(root, FEATURE, environ={})
    second_trace = build_feature_trace_graph(root, FEATURE, environ={})
    assert first_trace.graph.to_json() == second_trace.graph.to_json()
    assert first_trace.as_dict() == second_trace.as_dict()
    assert not [gap for gap in first_trace.gaps if "contract" in gap.kind]

    explicit_contract_edges = [
        edge
        for edge in first_trace.graph.edges
        if edge.metadata.get("contract_trace_role") == "link"
    ]
    assert len(explicit_contract_edges) == 6
    assert {edge.relation for edge in explicit_contract_edges} >= {
        TraceRelation.REFERENCES,
        TraceRelation.VERIFIED_BY,
        TraceRelation.APPROVED_BY,
        TraceRelation.EVIDENCED_BY,
    }
    assert all(edge.metadata["decision_sha256"] == allowed.sha256 for edge in explicit_contract_edges)
    assert all(edge.metadata["diff_sha256"] == public_diff.sha256 for edge in explicit_contract_edges)

    assert trace_main(["coverage", FEATURE, "--path", str(root), "--json"]) == 0
    coverage = json.loads(capsys.readouterr().out)
    assert coverage["apiVersion"] == "sdai.trace-coverage/v1"
    assert coverage["requirements_uncovered"] == 0
    assert coverage["contract_trace"]["sources_total"] == 4
    assert coverage["contract_trace"]["links"] == 6
    assert coverage["contract_trace"]["gaps"] == 0


def test_v016_upgrade_compatibility_and_fail_closed_contract_inputs(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    root.mkdir()
    _write(root / ".sdai" / "config.yaml", "{}\n")
    feature = root / "specs" / "changes" / FEATURE
    _write(feature / "requirements.md", "# Requirements\n\n- FR-016: Legacy project remains valid.\n")
    _write(feature / "tasks.md", "# Tasks\n\n- [ ] TASK-016: Verify FR-016.\n")

    empty_index = build_contract_trace_index(root)
    assert empty_index.nodes == ()
    assert empty_index.edges == ()
    assert empty_index.gaps == ()
    legacy_trace = build_feature_trace_graph(root, FEATURE, environ={})
    assert not any(
        node.metadata.get("contract_trace_role") in {"source", "symbol"}
        for node in legacy_trace.graph.nodes
    )

    _write(
        root / ".sdai" / "contracts.yaml",
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: unsafe
    kind: openapi
    path: ../outside.yaml
""",
    )
    with pytest.raises(ContractError, match="SDAI-CONTRACT-SOURCE-002"):
        discover_contracts(root)

    _write(
        root / ".sdai" / "contracts.yaml",
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: unknown
    kind: graphql
    path: contracts/schema.graphql
""",
    )
    with pytest.raises(ContractError, match="SDAI-CONTRACT-SOURCE-004"):
        discover_contracts(root)


def test_v016_release_gate_keeps_historical_gates_and_full_matrix_enabled() -> None:
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
    }
    assert all((root / path).is_file() for path in required)
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert 'python-version: ["3.11", "3.12"]' in workflow
    assert "pytest -q" in workflow
    assert "pytest -q tests/" not in workflow

    release_doc = (root / "docs" / "releases" / "0.16-release-evidence.md").read_text(encoding="utf-8")
    capability_doc = (root / "docs" / "CONTRACT-ENGINEERING.md").read_text(encoding="utf-8")
    assert "0.6–0.15" in release_doc
    assert "sdai contract gate" in capability_doc
    assert "sdai.contract-policy-decision/v1" in capability_doc
    assert "projects without `.sdai/contracts.yaml`" in capability_doc
