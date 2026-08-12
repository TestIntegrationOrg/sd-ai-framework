from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sdai.artifact_state import (
    ArtifactEvidenceInput,
    ArtifactFreshness,
    ArtifactStateError,
    evaluate_artifact_states,
    record_artifact_state,
)
from sdai.artifact_schemas import load_artifact_schema_graph


FEATURE = "STATE-100"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _write_repo_schema(root: Path) -> Path:
    payload = {
        "apiVersion": "sdai/v1",
        "kind": "ArtifactSchema",
        "metadata": {"id": "state-fixtures", "version": "1.0.0"},
        "spec": {
            "artifacts": [
                {
                    "id": "runbook-root",
                    "path": "specs/changes/{feature}/runbook-root.md",
                    "type": "markdown",
                    "required": False,
                    "depends_on": [],
                    "applies_to": ["standard", "critical", "regulated"],
                },
                {
                    "id": "runbook",
                    "path": "specs/changes/{feature}/runbook.md",
                    "type": "markdown",
                    "required": False,
                    "depends_on": ["runbook-root"],
                    "applies_to": ["standard", "critical", "regulated"],
                },
            ]
        },
    }
    path = root / ".sdai" / "schemas" / "state-fixtures.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _artifact_path(root: Path, artifact_id: str) -> Path:
    names = {
        "requirements": "requirements.md",
        "architecture": "architecture.md",
        "plan": "plan.md",
        "tasks": "tasks.md",
        "tests": "tests.md",
        "verification": "verification.md",
        "runbook-root": "runbook-root.md",
        "runbook": "runbook.md",
    }
    return root / "specs" / "changes" / FEATURE / names[artifact_id]


def _create_standard_artifacts(root: Path) -> None:
    _write_repo_schema(root)
    for artifact_id in (
        "requirements",
        "architecture",
        "plan",
        "tasks",
        "tests",
        "runbook-root",
        "runbook",
    ):
        _write(
            _artifact_path(root, artifact_id),
            f"# {artifact_id}\n\ncontent for {artifact_id} café Δ\n",
        )
    _write(
        _artifact_path(root, "verification"),
        "version: 1\nstatus: verified\n",
    )


def _record_all(root: Path, *, architecture_evidence: tuple[ArtifactEvidenceInput, ...] = ()) -> None:
    report = evaluate_artifact_states(root, FEATURE, risk="standard", environ={})
    for artifact_id in report.topological_order:
        evidence = architecture_evidence if artifact_id == "architecture" else ()
        record_artifact_state(
            root,
            FEATURE,
            artifact_id,
            risk="standard",
            evidence=evidence,
            environ={},
        )


def test_requirement_change_stales_only_dependent_branch_transitively(tmp_path: Path) -> None:
    _create_standard_artifacts(tmp_path)
    _record_all(tmp_path)

    initial = evaluate_artifact_states(tmp_path, FEATURE, risk="standard", environ={})
    assert all(item.freshness is ArtifactFreshness.FRESH for item in initial.states)

    requirements = _artifact_path(tmp_path, "requirements")
    requirements.write_text(
        requirements.read_text(encoding="utf-8") + "\nnew requirement\n",
        encoding="utf-8",
        newline="\n",
    )

    changed = evaluate_artifact_states(tmp_path, FEATURE, risk="standard", environ={})
    states = changed.by_id()

    for artifact_id in (
        "requirements",
        "architecture",
        "plan",
        "tasks",
        "tests",
        "verification",
    ):
        assert states[artifact_id].freshness is ArtifactFreshness.STALE, artifact_id
    assert states["runbook-root"].freshness is ArtifactFreshness.FRESH
    assert states["runbook"].freshness is ArtifactFreshness.FRESH
    assert any("content hash changed" in reason for reason in states["requirements"].reasons)
    assert any("dependency state is stale" in reason for reason in states["tasks"].reasons)


def test_missing_upstream_content_blocks_existing_dependents(tmp_path: Path) -> None:
    _create_standard_artifacts(tmp_path)
    _record_all(tmp_path)
    _artifact_path(tmp_path, "requirements").unlink()

    report = evaluate_artifact_states(tmp_path, FEATURE, risk="standard", environ={})
    states = report.by_id()

    assert states["requirements"].freshness is ArtifactFreshness.MISSING
    assert states["architecture"].freshness is ArtifactFreshness.BLOCKED
    assert states["plan"].freshness is ArtifactFreshness.BLOCKED
    assert states["tasks"].freshness is ArtifactFreshness.BLOCKED
    assert states["runbook"].freshness is ArtifactFreshness.FRESH


def test_recording_downstream_artifact_requires_fresh_dependency_evidence(tmp_path: Path) -> None:
    _create_standard_artifacts(tmp_path)

    with pytest.raises(
        ArtifactStateError,
        match="SDAI-STATE-004.*dependencies are fresh.*requirements",
    ):
        record_artifact_state(
            tmp_path,
            FEATURE,
            "architecture",
            risk="standard",
            environ={},
        )

    record_artifact_state(tmp_path, FEATURE, "requirements", risk="standard", environ={})
    record_artifact_state(tmp_path, FEATURE, "architecture", risk="standard", environ={})
    assert (
        evaluate_artifact_states(tmp_path, FEATURE, risk="standard", environ={})
        .by_id()["architecture"]
        .freshness
        is ArtifactFreshness.FRESH
    )


def test_bound_approval_or_validation_file_change_invalidates_artifact_and_downstream(
    tmp_path: Path,
) -> None:
    _create_standard_artifacts(tmp_path)
    approval = _write(
        tmp_path / "specs" / "changes" / FEATURE / "approvals" / "architecture.yaml",
        "version: 1\nstatus: approved\n",
    )
    validation = _write(
        tmp_path / "specs" / "changes" / FEATURE / "quality" / "architecture.yaml",
        "version: 1\nstatus: passed\n",
    )
    bindings = (
        ArtifactEvidenceInput(
            "approval",
            "architecture-approval",
            approval.relative_to(tmp_path).as_posix(),
        ),
        ArtifactEvidenceInput(
            "validation",
            "architecture-validation",
            validation.relative_to(tmp_path).as_posix(),
        ),
    )
    _record_all(tmp_path, architecture_evidence=bindings)

    validation.write_text(
        "version: 1\nstatus: failed\n",
        encoding="utf-8",
        newline="\n",
    )

    report = evaluate_artifact_states(tmp_path, FEATURE, risk="standard", environ={})
    states = report.by_id()
    architecture = states["architecture"]

    assert states["requirements"].freshness is ArtifactFreshness.FRESH
    assert architecture.freshness is ArtifactFreshness.STALE
    assert states["plan"].freshness is ArtifactFreshness.STALE
    evidence = {item.id: item for item in architecture.evidence}
    assert evidence["architecture-approval"].fresh is True
    assert evidence["architecture-validation"].fresh is False
    assert evidence["architecture-validation"].reason == "evidence source hash changed"


def test_deleted_bound_evidence_file_is_stale_not_silently_ignored(tmp_path: Path) -> None:
    _create_standard_artifacts(tmp_path)
    approval = _write(
        tmp_path / "specs" / "changes" / FEATURE / "approvals" / "architecture.yaml",
        "version: 1\nstatus: approved\n",
    )
    _record_all(
        tmp_path,
        architecture_evidence=(
            ArtifactEvidenceInput(
                "approval",
                "architecture-approval",
                approval.relative_to(tmp_path).as_posix(),
            ),
        ),
    )
    approval.unlink()

    architecture = evaluate_artifact_states(
        tmp_path,
        FEATURE,
        risk="standard",
        environ={},
    ).by_id()["architecture"]

    assert architecture.freshness is ArtifactFreshness.STALE
    assert architecture.evidence[0].current_sha256 is None
    assert architecture.evidence[0].reason == "evidence source is missing"


def test_text_hashes_normalize_windows_and_unix_line_endings(tmp_path: Path) -> None:
    _create_standard_artifacts(tmp_path)
    record_artifact_state(tmp_path, FEATURE, "requirements", risk="standard", environ={})
    path = _artifact_path(tmp_path, "requirements")
    normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    path.write_bytes(normalized.replace("\n", "\r\n").encode("utf-8"))

    state = evaluate_artifact_states(
        tmp_path,
        FEATURE,
        risk="standard",
        environ={},
    ).by_id()["requirements"]

    assert state.freshness is ArtifactFreshness.FRESH
    assert state.current_sha256 == state.recorded_sha256


def test_malformed_state_record_fails_closed_with_stable_error(tmp_path: Path) -> None:
    _create_standard_artifacts(tmp_path)
    record = (
        tmp_path
        / "specs"
        / "changes"
        / FEATURE
        / ".sdai"
        / "artifact-state"
        / "requirements.yaml"
    )
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        "version: 1\nartifact_id: requirements\nunknown: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactStateError, match="SDAI-STATE-002.*unknown field"):
        evaluate_artifact_states(tmp_path, FEATURE, risk="standard", environ={})


def test_state_json_is_deterministic_and_contains_no_provider_decision(tmp_path: Path) -> None:
    _create_standard_artifacts(tmp_path)
    _record_all(tmp_path)

    first = evaluate_artifact_states(tmp_path, FEATURE, risk="standard", environ={}).to_json()
    second = evaluate_artifact_states(tmp_path, FEATURE, risk="standard", environ={}).to_json()

    assert first == second
    payload = json.loads(first)
    assert payload["feature_id"] == FEATURE
    assert payload["counts"]["fresh"] == len(payload["artifacts"])
    assert "provider" not in first.casefold()
    assert "model" not in first.casefold()
