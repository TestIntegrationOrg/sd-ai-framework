from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from sdai.agent_platform.models import Capability, ExecutionMode
from sdai.agent_platform.runtime import AgentRuntime
from sdai.audited_orchestrator import AuditedOrchestrator
from sdai.audit_export import build_audit_export_package
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor, AuditBinding
from sdai.audit_sinks import LocalFilesystemAuditSink, handoff_audit_export
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project
from sdai.trace_builder import build_feature_trace_graph
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance
from sdai.version_entrypoint import main as sdai_main


FEATURE = "RELEASE-AUDIT-019"
COMMIT = "a" * 40
SECRET_CONTEXT = "release-gate-secret-context"
SECRET_OUTPUT = "release-gate-secret-output"


class _DeterministicProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        return SECRET_OUTPUT


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _sha(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    init_project(root)
    # The release journey must exercise the supported upgrade path before any
    # 0.19 provenance is created.
    assert sdai_main(["upgrade", "--path", str(root)]) == 0

    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        "# Requirements\n\n- FR-019: Produce an inspectable, immutable audit provenance chain.\n",
    )
    _write(
        root / "src" / "signing.py",
        "# Trace: FR-019\n\ndef sign() -> None:\n    pass\n",
    )
    _write(
        root / ".sdai" / "agents" / "release-auditor.agent.md",
        """---
name: release-auditor
description: Deterministic 0.19 release-gate agent
capabilities: [coding]
skills: [secure-coding]
profile: codex
execution_mode: advisory
---
Review the release input without persisting raw prompt or output content.
""",
    )
    _write(
        root / ".sdai" / "workflows" / "release-audit.yaml",
        """version: 3
name: release-audit
validation_mode: standard
steps:
  - id: specification
    type: deterministic
    action: specify
""",
    )
    return root


def _record_trace_evidence(root: Path) -> TraceEvidence:
    feature = root / "specs" / "changes" / FEATURE
    source = root / "src" / "signing.py"
    record = TraceEvidence(
        evidence_id="EVIDENCE-RELEASE-019",
        kind=EvidenceKind.TEST,
        status=EvidenceStatus.PASSED,
        subject="requirement:FR-019",
        git_commit=COMMIT,
        bindings=(
            EvidenceBinding(EvidenceBindingKind.SOURCE, "src/signing.py", _sha(source)),
        ),
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/evidence/release.json",
                1,
                detail="0.19 release gate",
            ),
        ),
        producer=EvidenceProducer("release-gate"),
        result={"passed": 1},
        command=("pytest", "-q", "tests/test_v019_audit_provenance_release_gate.py"),
        tool="pytest",
    )
    evidence_path = _write(feature / "evidence" / "release.json", record.to_json())
    AuditLedger(root, FEATURE).append(
        category="evidence",
        actor=AuditActor("system", "release-gate"),
        action=AuditAction("release.evidence.recorded", f"feature:{FEATURE}"),
        bindings=(
            AuditBinding("evidence", evidence_path.relative_to(root).as_posix(), _sha(evidence_path)),
        ),
        metadata={"status": "passed"},
        occurred_at="2026-08-19T06:00:00Z",
    )
    return record


def _ledger_bytes(root: Path) -> bytes:
    return (
        root
        / "specs"
        / "changes"
        / FEATURE
        / ".sdai"
        / "audit"
        / "events.jsonl"
    ).read_bytes()


def test_v019_release_journey_is_hash_consistent_read_only_and_privacy_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)
    capsys.readouterr()
    provider = _DeterministicProvider()
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: provider),
    )

    runtime = AgentRuntime(root)
    invocation = runtime.build_explicit_context_invocation(
        FEATURE,
        Capability.CODING,
        SECRET_CONTEXT,
        agent_name="release-auditor",
        mode=ExecutionMode.ADVISORY,
    )
    result = runtime.execute_invocation(invocation)
    assert result.output == SECRET_OUTPUT
    assert len(provider.calls) == 1

    workflow = AuditedOrchestrator(root, agent_runtime=runtime).run_workflow(
        FEATURE,
        "release-audit",
    )
    assert [item.status for item in workflow] == ["completed"]
    assert len(provider.calls) == 1  # deterministic workflow never re-executes the provider

    typed_evidence = _record_trace_evidence(root)
    ledger = AuditLedger(root, FEATURE)
    snapshot = ledger.verify()
    source_bytes = _ledger_bytes(root)

    first_trace = build_feature_trace_graph(root, FEATURE, environ={}).graph
    second_trace = build_feature_trace_graph(root, FEATURE, environ={}).graph
    assert first_trace.to_json() == second_trace.to_json()
    ledger_node = next(
        node for node in first_trace.nodes if node.entity_id == f"audit-ledger:{FEATURE}"
    )
    assert ledger_node.metadata["head_sha256"] == snapshot.head_sha256
    assert ledger_node.metadata["export_sha256"] == snapshot.export_sha256
    assert any(node.entity_id == typed_evidence.evidence_id for node in first_trace.nodes)

    assert sdai_main(["audit", FEATURE, "--json", "--path", str(root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ledgerHeadSha256"] == snapshot.head_sha256
    assert report["exportSha256"] == snapshot.export_sha256
    assert report["eventCount"] == snapshot.event_count

    first_package = build_audit_export_package(root, FEATURE, chunk_size=1024)
    second_package = build_audit_export_package(root, FEATURE, chunk_size=1024)
    assert first_package.manifest.to_json() == second_package.manifest.to_json()
    assert first_package.chunk_bytes == second_package.chunk_bytes
    assert first_package.manifest.ledger_head_sha256 == snapshot.head_sha256
    assert first_package.manifest.export_sha256 == snapshot.export_sha256

    sink = LocalFilesystemAuditSink(root / "audit-export-sink")
    accepted = handoff_audit_export(root, FEATURE, sink, package=first_package)
    replay = handoff_audit_export(root, FEATURE, sink, package=second_package)
    assert accepted.status == "accepted"
    assert replay.status == "already-present"
    assert accepted.export_id == replay.export_id
    assert accepted.manifest_sha256 == replay.manifest_sha256
    assert accepted.chunk_sha256 == replay.chunk_sha256

    # Inspection, trace projection, packaging and sink handoff are read-only with
    # respect to the authoritative source ledger.
    assert _ledger_bytes(root) == source_bytes
    assert AuditLedger(root, FEATURE).verify() == snapshot

    privacy_surfaces = "\n".join(
        (
            source_bytes.decode("utf-8"),
            json.dumps(report, sort_keys=True),
            first_trace.to_json(),
            first_package.manifest.to_json(),
            accepted.to_json(),
            replay.to_json(),
        )
    )
    assert SECRET_CONTEXT not in privacy_surfaces
    assert SECRET_OUTPUT not in privacy_surfaces
    assert "identityVerified" not in privacy_surfaces
    assert "enterpriseIdentity" not in privacy_surfaces


def test_v019_release_gate_keeps_adversarial_and_historical_gates_in_unfiltered_ci() -> None:
    root = Path(__file__).resolve().parents[1]
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "push:" in ci and "branches: [main]" in ci
    assert "pull_request:" in ci
    assert "pytest -q" in ci
    assert "-k " not in ci and "--ignore" not in ci

    required = {
        "test_agent_audit_provenance_v019.py",
        "test_workflow_audit_provenance_v019.py",
        "test_audit_provenance_foundation_v019.py",
        "test_audit_trace_core_v019.py",
        "test_audit_report_v019.py",
        "test_audit_export_v019.py",
        "test_audit_export_record_count_v019.py",
        "test_audit_sinks_v019.py",
        "test_audit_sink_concurrency_v019.py",
        "test_audit_sink_path_safety_v019.py",
        "test_v017_architecture_drift_release_gate.py",
    }
    tests = root / "tests"
    missing = sorted(name for name in required if not (tests / name).is_file())
    assert missing == []

    # 0.18 is deliberately held. The 0.19 gate proves identity-independent local
    # provenance only and must not turn local approval assertions into enterprise
    # identity claims.
    docs = (root / "docs" / "AUDIT-PROVENANCE.md").read_text(encoding="utf-8")
    assert "0.18" in docs
    assert "deferred" in docs.casefold() or "held" in docs.casefold()
