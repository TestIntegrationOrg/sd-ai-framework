from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.trace_evidence import (
    TRACE_EVIDENCE_API_VERSION,
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
    TraceEvidenceError,
    load_trace_evidence,
    validate_trace_evidence,
)
from sdai.trace_graph import TraceProvenance


COMMIT = "a" * 40
DIGEST = "sha256:" + "1" * 64


def _record(
    *,
    kind: EvidenceKind = EvidenceKind.TEST,
    status: EvidenceStatus = EvidenceStatus.PASSED,
    producer: EvidenceProducer | None = None,
    result: dict[str, object] | None = None,
    command: tuple[str, ...] = ("python", "-m", "pytest", "tests/test_signing.py"),
) -> TraceEvidence:
    return TraceEvidence(
        evidence_id="EVIDENCE-001",
        kind=kind,
        status=status,
        subject="requirement:FR-001",
        git_commit=COMMIT,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.SOURCE,
                "src/café/signing.py",
                DIGEST,
            ),
        ),
        provenance=(
            TraceProvenance(
                "specs/changes/TRACE-100/evidence/test.json",
                1,
                detail="pytest verification",
            ),
        ),
        producer=producer or EvidenceProducer("tester", "codex", "gpt-test"),
        result=result or {"passed": 42, "failed": 0},
        command=command,
        tool="pytest",
    )


def test_contract_supports_all_required_evidence_kinds_and_finite_statuses() -> None:
    assert {item.value for item in EvidenceKind} == {
        "execution",
        "test",
        "quality",
        "security",
        "approval",
        "review",
        "operational",
    }
    assert {item.value for item in EvidenceStatus} == {
        "passed",
        "failed",
        "blocked",
        "recorded",
    }
    for kind in EvidenceKind:
        record = _record(kind=kind)
        assert record.as_dict()["apiVersion"] == TRACE_EVIDENCE_API_VERSION
        assert record.kind is kind
        assert record.truth_sha256.startswith("sha256:")
        assert record.sha256.startswith("sha256:")


def test_provider_and_model_metadata_do_not_change_evidence_truth() -> None:
    first = _record(producer=EvidenceProducer("tester", "codex", "model-a"))
    second = _record(producer=EvidenceProducer("tester", "claude", "model-b"))

    assert first.truth_dict() == second.truth_dict()
    assert first.truth_sha256 == second.truth_sha256
    assert first.sha256 != second.sha256
    assert first.as_dict()["producer"] != second.as_dict()["producer"]


def test_semantic_role_is_part_of_producer_metadata_not_provider_identity() -> None:
    record = _record(producer=EvidenceProducer("security-reviewer", None, None))
    assert record.producer.semantic_role == "security-reviewer"
    assert record.producer.provider is None
    assert record.producer.model is None


def test_binding_order_is_canonical_and_duplicate_conflicts_fail_closed() -> None:
    source = EvidenceBinding(EvidenceBindingKind.SOURCE, "src/z.py", "sha256:" + "2" * 64)
    artifact = EvidenceBinding(EvidenceBindingKind.ARTIFACT, "specs/changes/TRACE-100/architecture.md", "sha256:" + "3" * 64)
    record = TraceEvidence(
        evidence_id="EVIDENCE-002",
        kind=EvidenceKind.QUALITY,
        status=EvidenceStatus.PASSED,
        subject="task:TASK-001",
        git_commit=COMMIT,
        bindings=(source, artifact),
        provenance=(TraceProvenance("evidence.json", 1),),
        producer=EvidenceProducer("code-reviewer"),
        result={"score": 100},
        command=("ruff", "check", "."),
        tool="ruff",
    )
    reversed_record = TraceEvidence(
        evidence_id="EVIDENCE-002",
        kind=EvidenceKind.QUALITY,
        status=EvidenceStatus.PASSED,
        subject="task:TASK-001",
        git_commit=COMMIT,
        bindings=(artifact, source),
        provenance=(TraceProvenance("evidence.json", 1),),
        producer=EvidenceProducer("code-reviewer"),
        result={"score": 100},
        command=("ruff", "check", "."),
        tool="ruff",
    )
    assert record.to_json() == reversed_record.to_json()

    with pytest.raises(TraceEvidenceError, match="conflicting binding"):
        TraceEvidence(
            evidence_id="EVIDENCE-003",
            kind=EvidenceKind.QUALITY,
            status=EvidenceStatus.PASSED,
            subject="task:TASK-001",
            git_commit=COMMIT,
            bindings=(
                source,
                EvidenceBinding(EvidenceBindingKind.SOURCE, "src/z.py", "sha256:" + "4" * 64),
            ),
            provenance=(TraceProvenance("evidence.json", 1),),
            producer=EvidenceProducer("code-reviewer"),
            command=("ruff", "check", "."),
        )


def test_unbound_invalid_hash_commit_and_nonportable_sources_fail_closed() -> None:
    with pytest.raises(TraceEvidenceError, match="at least one SHA-256 content binding"):
        TraceEvidence(
            evidence_id="EVIDENCE-004",
            kind=EvidenceKind.EXECUTION,
            status=EvidenceStatus.PASSED,
            subject="task:TASK-001",
            git_commit=COMMIT,
            bindings=(),
            provenance=(TraceProvenance("evidence.json", 1),),
            producer=EvidenceProducer("developer"),
            command=("python", "build.py"),
        )

    with pytest.raises(TraceEvidenceError, match="invalid SHA-256"):
        EvidenceBinding(EvidenceBindingKind.SOURCE, "src/app.py", "not-a-hash")
    with pytest.raises(TraceEvidenceError, match="invalid Git commit"):
        _record().___class__(  # type: ignore[attr-defined]
            evidence_id="E",
            kind=EvidenceKind.TEST,
            status=EvidenceStatus.PASSED,
            subject="test:T",
            git_commit="deadbeef",
            bindings=(EvidenceBinding(EvidenceBindingKind.TEST, "tests/t.py", DIGEST),),
            provenance=(TraceProvenance("evidence.json", 1),),
            producer=EvidenceProducer("tester"),
            command=("pytest",),
        )
    with pytest.raises(TraceEvidenceError, match="repository-relative POSIX"):
        EvidenceBinding(EvidenceBindingKind.SOURCE, "C:\\src\\app.py", DIGEST)
    with pytest.raises(TraceEvidenceError, match="unsafe binding source"):
        EvidenceBinding(EvidenceBindingKind.SOURCE, "../app.py", DIGEST)


def test_command_is_argument_array_not_shell_text() -> None:
    with pytest.raises(TraceEvidenceError, match="argument array"):
        TraceEvidence(
            evidence_id="EVIDENCE-005",
            kind=EvidenceKind.EXECUTION,
            status=EvidenceStatus.PASSED,
            subject="task:TASK-001",
            git_commit=COMMIT,
            bindings=(EvidenceBinding(EvidenceBindingKind.SOURCE, "src/app.py", DIGEST),),
            provenance=(TraceProvenance("evidence.json", 1),),
            producer=EvidenceProducer("developer"),
            command="python build.py",  # type: ignore[arg-type]
        )


def test_result_rejects_nonfinite_and_unsupported_values() -> None:
    with pytest.raises(TraceEvidenceError, match="non-finite"):
        _record(result={"coverage": float("nan")})
    with pytest.raises(TraceEvidenceError, match="unsupported JSON type"):
        _record(result={"bad": {1, 2, 3}})  # type: ignore[dict-item]


def test_round_trip_is_strict_and_tampering_is_rejected() -> None:
    record = _record()
    restored = validate_trace_evidence(record.to_json())
    assert restored.to_json() == record.to_json()
    assert validate_trace_evidence(record).sha256 == record.sha256

    tampered = json.loads(record.to_json())
    tampered["result"]["failed"] = 1
    with pytest.raises(TraceEvidenceError, match="truth SHA-256 does not match"):
        validate_trace_evidence(tampered)

    unknown = json.loads(record.to_json())
    unknown["unexpected"] = True
    with pytest.raises(TraceEvidenceError, match="fields do not match"):
        validate_trace_evidence(unknown)


def test_producer_change_requires_record_hash_but_not_truth_hash_change() -> None:
    record = _record()
    payload = json.loads(record.to_json())
    original_truth = payload["truth_sha256"]
    payload["producer"]["provider"] = "another-provider"
    with pytest.raises(TraceEvidenceError, match="SHA-256 does not match canonical record"):
        TraceEvidence.from_mapping(payload)
    payload["sha256"] = _record(
        producer=EvidenceProducer("tester", "another-provider", "gpt-test")
    ).sha256
    restored = TraceEvidence.from_mapping(payload)
    assert restored.truth_sha256 == original_truth


def test_utf8_repository_evidence_load_and_path_security(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    record = _record()
    evidence_dir = root / "specs" / "changes" / "TRACE-Ω" / "evidence"
    evidence_dir.mkdir(parents=True)
    evidence_path = evidence_dir / "résultat.json"
    evidence_path.write_text(record.to_json(), encoding="utf-8")

    loaded = load_trace_evidence(root, evidence_path)
    assert loaded.to_json() == record.to_json()

    outside = tmp_path / "outside.json"
    outside.write_text(record.to_json(), encoding="utf-8")
    with pytest.raises(TraceEvidenceError, match="inside the project root"):
        load_trace_evidence(root, outside)


def test_loader_rejects_invalid_utf8_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    bad = root / "bad.json"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(TraceEvidenceError, match="invalid trace evidence JSON"):
        load_trace_evidence(root, bad)

    target = root / "target.json"
    target.write_text(_record().to_json(), encoding="utf-8")
    link = root / "link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this test environment")
    with pytest.raises(TraceEvidenceError, match="symlink"):
        load_trace_evidence(root, link)
