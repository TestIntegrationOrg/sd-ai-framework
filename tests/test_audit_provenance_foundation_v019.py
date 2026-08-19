from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

import sdai.audit_ledger as audit_ledger_module
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import (
    AUDIT_EVENT_API_VERSION,
    AuditAction,
    AuditActor,
    AuditBinding,
    AuditEvent,
    AuditExecution,
    AuditProvenanceError,
)


FEATURE = "AUDIT-233"
ZERO = "sha256:" + "0" * 64


def _workspace(tmp_path: Path, *, legacy: bool = False) -> Path:
    root = tmp_path / "project"
    feature = root / "specs" / (FEATURE if legacy else f"changes/{FEATURE}")
    feature.mkdir(parents=True)
    return root


def _actor() -> AuditActor:
    return AuditActor(
        "ai",
        "agent:implementation",
        semantic_role="implementer",
        provider="openai",
        model="gpt-test",
    )


def _action(kind: str = "task.implemented") -> AuditAction:
    return AuditAction(kind, "TASK-233", "deterministic audit foundation")


def _binding(kind: str, source: str, digit: str) -> AuditBinding:
    return AuditBinding(kind, source, "sha256:" + digit * 64)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _rehash(raw: dict[str, object]) -> dict[str, object]:
    body = dict(raw)
    body.pop("sha256", None)
    raw["sha256"] = "sha256:" + sha256(_canonical(body)).hexdigest()
    return raw


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links are unavailable on this runner: {exc}")


def test_event_hash_is_canonical_across_input_order_and_timezone() -> None:
    first = AuditEvent.create(
        sequence=1,
        feature_id=FEATURE,
        category="ai",
        occurred_at="2026-08-18T12:00:00-05:00",
        actor=_actor(),
        action=_action(),
        execution=AuditExecution(
            run_id="run-233",
            workflow="enterprise",
            task_id="TASK-233",
            git_commit="a" * 40,
            workspace="worktrees/run-233",
        ),
        bindings=(
            _binding("output", "artifacts/result.json", "2"),
            _binding("input", "specs/changes/AUDIT-233/requirements.md", "1"),
        ),
        metadata={"z": [3, 2, 1], "a": {"safe": True}},
    )
    second = AuditEvent.create(
        sequence=1,
        feature_id=FEATURE,
        category="ai",
        occurred_at="2026-08-18T17:00:00Z",
        actor=_actor(),
        action=_action(),
        execution=AuditExecution(
            run_id="run-233",
            workflow="enterprise",
            task_id="TASK-233",
            git_commit="A" * 40,
            workspace="worktrees/run-233",
        ),
        bindings=(
            _binding("input", "specs/changes/AUDIT-233/requirements.md", "1"),
            _binding("output", "artifacts/result.json", "2"),
        ),
        metadata={"a": {"safe": True}, "z": [3, 2, 1]},
    )

    assert first.occurred_at == "2026-08-18T17:00:00.000000Z"
    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert [item.kind for item in first.bindings] == ["input", "output"]


def test_append_replay_verify_and_export_current_workspace(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ledger = AuditLedger(root, FEATURE)

    first = ledger.append(
        category="human",
        actor=AuditActor("human", "developer@example.invalid", semantic_role="author"),
        action=AuditAction("requirement.updated", "FR-233", "clarify audit semantics"),
        bindings=(_binding("input", "specs/changes/AUDIT-233/requirements.md", "1"),),
        occurred_at="2026-08-18T17:00:00Z",
    )
    second = ledger.append(
        category="ai",
        actor=_actor(),
        action=_action(),
        execution=AuditExecution(run_id="run-233", task_id="TASK-233"),
        bindings=(_binding("output", "artifacts/result.json", "2"),),
        metadata={"attempt": 1},
        occurred_at="2026-08-18T17:01:00Z",
    )

    assert ledger.events_path == root / "specs" / "changes" / FEATURE / ".sdai" / "audit" / "events.jsonl"
    assert first.sequence == 1
    assert first.previous_sha256 == ZERO
    assert second.sequence == 2
    assert second.previous_sha256 == first.sha256
    assert ledger.read() == (first, second)

    exported = ledger.export_jsonl()
    assert exported.endswith(b"\n")
    assert exported == ledger.events_path.read_bytes()
    lines = exported.splitlines()
    assert [json.loads(line)["eventId"] for line in lines] == [f"{FEATURE}:00000001", f"{FEATURE}:00000002"]

    snapshot = ledger.verify()
    assert snapshot.event_count == 2
    assert snapshot.head_sha256 == second.sha256
    assert snapshot.export_sha256 == "sha256:" + sha256(exported).hexdigest()
    assert snapshot.to_dict()["apiVersion"] == "sdai.audit-ledger/v1"


def test_legacy_workspace_supported_but_dual_workspace_is_ambiguous(tmp_path: Path) -> None:
    root = _workspace(tmp_path, legacy=True)
    ledger = AuditLedger(root, FEATURE)
    assert ledger.events_path == root / "specs" / FEATURE / ".sdai" / "audit" / "events.jsonl"

    (root / "specs" / "changes" / FEATURE).mkdir(parents=True)
    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-004.*ambiguous"):
        AuditLedger(root, FEATURE)


def test_missing_feature_workspace_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-004.*does not exist"):
        AuditLedger(root, FEATURE)


def test_ai_provider_metadata_is_provenance_not_identity_authority() -> None:
    event = AuditEvent.create(
        sequence=1,
        feature_id=FEATURE,
        category="ai",
        occurred_at="2026-08-18T17:00:00Z",
        actor=_actor(),
        action=_action(),
    )
    actor = event.to_dict()["actor"]
    assert actor == {
        "kind": "ai",
        "subject": "agent:implementation",
        "semanticRole": "implementer",
        "provider": "openai",
        "model": "gpt-test",
    }
    assert "verified" not in actor
    assert "identityVerified" not in actor
    assert "authorized" not in actor

    raw = event.to_dict()
    raw_actor = dict(raw["actor"])
    raw_actor["identityVerified"] = True
    raw["actor"] = raw_actor
    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-005.*actor fields"):
        AuditEvent.from_mapping(raw)


def test_secret_like_metadata_is_rejected_recursively(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ledger = AuditLedger(root, FEATURE)

    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-003.*client_secret"):
        ledger.append(
            category="system",
            actor=AuditActor("system", "sdai"),
            action=AuditAction("policy.evaluated", "feature:AUDIT-233"),
            metadata={"safe": {"client_secret": "must-never-persist"}},
            occurred_at="2026-08-18T17:00:00Z",
        )

    assert ledger.verify().event_count == 0
    assert not ledger.events_path.exists()


@pytest.mark.parametrize(
    "source",
    ["../outside", "/absolute/file", "C:/windows/path", "safe/../outside", "safe\\windows"],
)
def test_binding_references_reject_unsafe_filesystem_syntax(source: str) -> None:
    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-002"):
        _binding("input", source, "1")


def test_duplicate_bindings_fail_closed() -> None:
    duplicate = _binding("input", "requirements.md", "1")
    with pytest.raises(AuditProvenanceError, match="duplicate audit binding"):
        AuditEvent.create(
            sequence=1,
            feature_id=FEATURE,
            category="system",
            occurred_at="2026-08-18T17:00:00Z",
            actor=AuditActor("system", "sdai"),
            action=AuditAction("verify.started", FEATURE),
            bindings=(duplicate, duplicate),
        )


def test_complete_event_mutation_is_detected(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ledger = AuditLedger(root, FEATURE)
    ledger.append(
        category="system",
        actor=AuditActor("system", "sdai"),
        action=AuditAction("verify.completed", FEATURE),
        metadata={"result": "passed"},
        occurred_at="2026-08-18T17:00:00Z",
    )

    raw = json.loads(ledger.events_path.read_text(encoding="utf-8"))
    raw["metadata"]["result"] = "failed"
    ledger.events_path.write_bytes(_canonical(raw) + b"\n")

    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-005.*hash mismatch"):
        ledger.verify()


def test_rehashed_history_still_cannot_break_previous_hash_chain(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ledger = AuditLedger(root, FEATURE)
    first = ledger.append(
        category="system",
        actor=AuditActor("system", "sdai"),
        action=AuditAction("run.started", FEATURE),
        occurred_at="2026-08-18T17:00:00Z",
    )
    ledger.append(
        category="system",
        actor=AuditActor("system", "sdai"),
        action=AuditAction("run.completed", FEATURE),
        occurred_at="2026-08-18T17:01:00Z",
    )

    lines = [json.loads(line) for line in ledger.events_path.read_text(encoding="utf-8").splitlines()]
    lines[1]["previousSha256"] = ZERO
    _rehash(lines[1])
    ledger.events_path.write_bytes(b"".join(_canonical(item) + b"\n" for item in lines))

    assert first.sha256 != ZERO
    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-005.*chain mismatch"):
        ledger.verify()


def test_noncanonical_reformatting_is_detected(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ledger = AuditLedger(root, FEATURE)
    event = ledger.append(
        category="system",
        actor=AuditActor("system", "sdai"),
        action=AuditAction("run.started", FEATURE),
        occurred_at="2026-08-18T17:00:00Z",
    )
    ledger.events_path.write_text(json.dumps(event.to_dict(), indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-005.*(?:invalid JSON|not canonical)"):
        ledger.verify()


def test_incomplete_crash_tail_is_recovered_only_during_append(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ledger = AuditLedger(root, FEATURE)
    first = ledger.append(
        category="system",
        actor=AuditActor("system", "sdai"),
        action=AuditAction("run.started", FEATURE),
        occurred_at="2026-08-18T17:00:00Z",
    )
    with ledger.events_path.open("ab") as stream:
        stream.write(b'{"apiVersion":"sdai.audit-event/v1"')

    with pytest.raises(AuditProvenanceError, match="incomplete final record"):
        ledger.read()

    second = ledger.append(
        category="system",
        actor=AuditActor("system", "sdai"),
        action=AuditAction("run.resumed", FEATURE),
        occurred_at="2026-08-18T17:01:00Z",
    )
    assert second.sequence == 2
    assert second.previous_sha256 == first.sha256
    assert ledger.verify().event_count == 2


def test_complete_json_without_newline_is_never_silently_discarded(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ledger = AuditLedger(root, FEATURE)
    ledger.append(
        category="system",
        actor=AuditActor("system", "sdai"),
        action=AuditAction("run.started", FEATURE),
        occurred_at="2026-08-18T17:00:00Z",
    )
    original = ledger.events_path.read_bytes().rstrip(b"\n")
    ledger.events_path.write_bytes(original)

    with pytest.raises(AuditProvenanceError, match="complete JSON but missing the canonical newline"):
        ledger.append(
            category="system",
            actor=AuditActor("system", "sdai"),
            action=AuditAction("run.completed", FEATURE),
            occurred_at="2026-08-18T17:01:00Z",
        )
    assert ledger.events_path.read_bytes() == original


def test_audit_directory_symlink_fails_closed(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    feature = root / "specs" / "changes" / FEATURE
    (feature / ".sdai").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(feature / ".sdai" / "audit", outside)

    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-004.*(?:symlink|escapes)"):
        AuditLedger(root, FEATURE)


def test_events_file_symlink_fails_closed(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ledger = AuditLedger(root, FEATURE)
    outside = tmp_path / "outside-events.jsonl"
    outside.write_text("", encoding="utf-8")
    _symlink_or_skip(ledger.events_path, outside)

    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-004.*symlink"):
        ledger.append(
            category="system",
            actor=AuditActor("system", "sdai"),
            action=AuditAction("run.started", FEATURE),
            occurred_at="2026-08-18T17:00:00Z",
        )


def test_event_contract_rejects_unknown_fields() -> None:
    event = AuditEvent.create(
        sequence=1,
        feature_id=FEATURE,
        category="system",
        occurred_at="2026-08-18T17:00:00Z",
        actor=AuditActor("system", "sdai"),
        action=AuditAction("verify.completed", FEATURE),
    )
    raw = event.to_dict()
    raw["unversionedExtension"] = True
    with pytest.raises(AuditProvenanceError, match="fields do not match"):
        AuditEvent.from_mapping(raw)
    assert event.to_dict()["apiVersion"] == AUDIT_EVENT_API_VERSION


def test_public_type_errors_fail_with_audit_error_not_raw_python_errors() -> None:
    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-002.*binding kind"):
        AuditBinding([], "requirements.md", "sha256:" + "1" * 64)  # type: ignore[arg-type]

    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-002.*actor/action"):
        AuditEvent.create(
            sequence=1,
            feature_id=FEATURE,
            category="system",
            occurred_at="2026-08-18T17:00:00Z",
            actor=object(),  # type: ignore[arg-type]
            action=AuditAction("verify.completed", FEATURE),
        )

    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-002.*metadata must be a mapping"):
        AuditEvent.create(
            sequence=1,
            feature_id=FEATURE,
            category="system",
            occurred_at="2026-08-18T17:00:00Z",
            actor=AuditActor("system", "sdai"),
            action=AuditAction("verify.completed", FEATURE),
            metadata=["not", "a", "mapping"],  # type: ignore[arg-type]
        )

    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-002.*bindings must be an iterable"):
        AuditEvent.create(
            sequence=1,
            feature_id=FEATURE,
            category="system",
            occurred_at="2026-08-18T17:00:00Z",
            actor=AuditActor("system", "sdai"),
            action=AuditAction("verify.completed", FEATURE),
            bindings=object(),  # type: ignore[arg-type]
        )


def test_crlf_reformatting_is_rejected_as_noncanonical(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    ledger = AuditLedger(root, FEATURE)
    ledger.append(
        category="system",
        actor=AuditActor("system", "sdai"),
        action=AuditAction("run.started", FEATURE),
        occurred_at="2026-08-18T17:00:00Z",
    )
    ledger.events_path.write_bytes(ledger.events_path.read_bytes().replace(b"\n", b"\r\n"))

    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-005.*not canonical"):
        ledger.verify()


def test_event_count_limit_blocks_before_writing_extra_record(tmp_path: Path, monkeypatch) -> None:
    root = _workspace(tmp_path)
    ledger = AuditLedger(root, FEATURE)
    monkeypatch.setattr(audit_ledger_module, "AUDIT_MAX_EVENTS", 1)
    ledger.append(
        category="system",
        actor=AuditActor("system", "sdai"),
        action=AuditAction("run.started", FEATURE),
        occurred_at="2026-08-18T17:00:00Z",
    )
    before = ledger.events_path.read_bytes()

    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-005.*event count limit"):
        ledger.append(
            category="system",
            actor=AuditActor("system", "sdai"),
            action=AuditAction("run.completed", FEATURE),
            occurred_at="2026-08-18T17:01:00Z",
        )
    assert ledger.events_path.read_bytes() == before


def test_invalid_feature_and_sequence_are_stable_audit_errors() -> None:
    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-002.*featureId"):
        AuditEvent.create(
            sequence=1,
            feature_id=object(),  # type: ignore[arg-type]
            category="system",
            occurred_at="2026-08-18T17:00:00Z",
            actor=AuditActor("system", "sdai"),
            action=AuditAction("verify.completed", FEATURE),
        )

    with pytest.raises(AuditProvenanceError, match="SDAI-AUDIT-002.*sequence"):
        AuditEvent.create(
            sequence="one",  # type: ignore[arg-type]
            feature_id=FEATURE,
            category="system",
            occurred_at="2026-08-18T17:00:00Z",
            actor=AuditActor("system", "sdai"),
            action=AuditAction("verify.completed", FEATURE),
        )
