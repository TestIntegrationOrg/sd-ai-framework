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
    producer: EvidenceProducer | None = None,
    result: dict[str, object] | None = None,
    command: tuple[str, ...] | None = ("python", "-m", "pytest"),
) -> TraceEvidence:
    return TraceEvidence(
        evidence_id="EVIDENCE-001",
        kind=kind,
        status=EvidenceStatus.PASSED,
        subject="requirement:FR-001",
        git_commit=COMMIT,
        bindings=(EvidenceBinding(EvidenceBindingKind.SOURCE, "src/café/signing.py", DIGEST),),
        provenance=(TraceProvenance("specs/changes/TRACE-100/evidence/test.json", 1),),
        producer=producer or EvidenceProducer("tester", "codex", "model-a"),
        result=result or {"passed": 42, "failed": 0},
        command=command,
        tool="pytest",
    )


def test_contract_has_required_kinds_and_is_versioned() -> None:
    assert {item.value for item in EvidenceKind} == {
        "execution", "test", "quality", "security", "approval", "review", "operational"
    }
    for kind in EvidenceKind:
        record = _record(kind=kind)
        assert record.as_dict()["apiVersion"] == TRACE_EVIDENCE_API_VERSION
        assert record.truth_sha256.startswith("sha256:")
        assert record.sha256.startswith("sha256:")


def test_provider_and_model_do_not_change_truth_identity() -> None:
    first = _record(producer=EvidenceProducer("tester", "codex", "model-a"))
    second = _record(producer=EvidenceProducer("tester", "claude", "model-b"))
    assert first.truth_dict() == second.truth_dict()
    assert first.truth_sha256 == second.truth_sha256
    assert first.sha256 != second.sha256


def test_binding_order_is_deterministic_and_conflicts_fail_closed() -> None:
    a = EvidenceBinding(EvidenceBindingKind.ARTIFACT, "specs/architecture.md", "sha256:" + "2" * 64)
    b = EvidenceBinding(EvidenceBindingKind.SOURCE, "src/app.py", "sha256:" + "3" * 64)
    base = dict(
        evidence_id="EVIDENCE-002",
        kind=EvidenceKind.QUALITY,
        status=EvidenceStatus.PASSED,
        subject="task:TASK-001",
        git_commit=COMMIT,
        provenance=(TraceProvenance("evidence.json", 1),),
        producer=EvidenceProducer("code-reviewer"),
        result={"score": 100},
        command=("ruff", "check", "."),
        tool="ruff",
    )
    assert TraceEvidence(bindings=(a, b), **base).to_json() == TraceEvidence(bindings=(b, a), **base).to_json()

    with pytest.raises(TraceEvidenceError, match="conflicting binding"):
        TraceEvidence(
            bindings=(b, EvidenceBinding(EvidenceBindingKind.SOURCE, "src/app.py", "sha256:" + "4" * 64)),
            **base,
        )


def test_unbound_invalid_hash_commit_and_paths_fail_closed() -> None:
    with pytest.raises(TraceEvidenceError, match="at least one SHA-256"):
        TraceEvidence(
            evidence_id="EVIDENCE-003",
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
        EvidenceBinding(EvidenceBindingKind.SOURCE, "src/app.py", "bad")
    with pytest.raises(TraceEvidenceError, match="invalid Git commit"):
        TraceEvidence(
            evidence_id="EVIDENCE-004",
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


def test_command_is_argv_and_optional_empty_command_round_trips() -> None:
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

    record = _record(command=None)
    assert TraceEvidence.from_json(record.to_json()).command == ()


def test_result_requires_finite_json() -> None:
    with pytest.raises(TraceEvidenceError, match="non-finite"):
        _record(result={"coverage": float("nan")})
    with pytest.raises(TraceEvidenceError, match="unsupported JSON type"):
        _record(result={"bad": {1, 2}})  # type: ignore[dict-item]


def test_round_trip_tamper_and_unknown_fields_are_rejected() -> None:
    record = _record()
    assert validate_trace_evidence(record.to_json()).to_json() == record.to_json()
    assert validate_trace_evidence(record).sha256 == record.sha256

    tampered = json.loads(record.to_json())
    tampered["result"]["failed"] = 1
    with pytest.raises(TraceEvidenceError, match="truth SHA-256 does not match"):
        validate_trace_evidence(tampered)

    unknown = json.loads(record.to_json())
    unknown["unexpected"] = True
    with pytest.raises(TraceEvidenceError, match="fields do not match"):
        validate_trace_evidence(unknown)


def test_provider_tamper_changes_record_hash_not_truth_hash() -> None:
    record = _record()
    payload = json.loads(record.to_json())
    truth = payload["truth_sha256"]
    payload["producer"]["provider"] = "another-provider"
    with pytest.raises(TraceEvidenceError, match="SHA-256 does not match canonical record"):
        TraceEvidence.from_mapping(payload)
    replacement = _record(producer=EvidenceProducer("tester", "another-provider", "model-a"))
    payload["sha256"] = replacement.sha256
    restored = TraceEvidence.from_mapping(payload)
    assert restored.truth_sha256 == truth


def test_utf8_loader_and_repository_boundary(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "specs" / "TRACE-Ω" / "résultat.json"
    path.parent.mkdir(parents=True)
    path.write_text(_record().to_json(), encoding="utf-8")
    assert load_trace_evidence(root, path).evidence_id == "EVIDENCE-001"

    outside = tmp_path / "outside.json"
    outside.write_text(_record().to_json(), encoding="utf-8")
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
        pytest.skip("symlinks unavailable")
    with pytest.raises(TraceEvidenceError, match="symlink"):
        load_trace_evidence(root, link)
