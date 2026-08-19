from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import sdai.artifact_state_cli as artifact_state_cli
import sdai.schema_cli as schema_cli
import sdai.skill_cli as skill_cli
import sdai.spec_cli as spec_cli
import sdai.tech_cli as tech_cli
from sdai.evals import EVAL_REPORT_API_VERSION, EvalReport
from sdai.workflow_cli import (
    WORKFLOW_LEGACY_DEFINITION_API_VERSION,
    _definition_payload,
)


class _State:
    def as_dict(self) -> dict[str, object]:
        return {"artifact_id": "requirements", "version": 1}


class _ArtifactReport:
    feature_id = "JSON-100"
    risk = "standard"
    states: tuple[object, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "counts": {"fresh": 1, "stale": 0, "missing": 0, "blocked": 0},
        }

    def by_id(self) -> dict[str, object]:
        return {"requirements": _State()}


class _SchemaArtifact:
    id = "requirements"

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "path": "requirements.md"}


class _SchemaGraph:
    artifacts: tuple[object, ...] = ()
    sources: tuple[str, ...] = ()
    topological_order: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {"version": 1, "artifacts": [], "topological_order": [], "edges": [], "sources": []}

    def by_id(self) -> dict[str, object]:
        return {"requirements": _SchemaArtifact()}


class _SimpleReport:
    def as_dict(self, **_: object) -> dict[str, object]:
        return {"version": 1, "status": "ok"}


class _ValidationReport(_SimpleReport):
    valid = True


class _ApprovalDecision(_SimpleReport):
    satisfied = True


class _PromotionPreview(_SimpleReport):
    eligible = True


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("status", artifact_state_cli.ARTIFACT_STATE_REPORT_API_VERSION),
        ("explain", artifact_state_cli.ARTIFACT_STATE_EXPLAIN_API_VERSION),
    ],
)
def test_artifact_json_is_machine_clean_and_versioned(
    action: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(artifact_state_cli, "evaluate_artifact_states", lambda *args, **kwargs: _ArtifactReport())
    args = SimpleNamespace(
        artifact_action=action,
        feature="JSON-100",
        artifact_id="requirements",
        risk="standard",
        domain=None,
        json=True,
    )

    assert artifact_state_cli.run_artifact_state_command(Path("."), args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apiVersion"] == expected
    assert payload["version"] == 1


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("list", schema_cli.ARTIFACT_SCHEMA_GRAPH_API_VERSION),
        ("validate", schema_cli.ARTIFACT_SCHEMA_GRAPH_API_VERSION),
        ("graph", schema_cli.ARTIFACT_SCHEMA_GRAPH_API_VERSION),
        ("show", schema_cli.ARTIFACT_SCHEMA_DEFINITION_API_VERSION),
    ],
)
def test_schema_json_is_machine_clean_and_versioned(
    action: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(schema_cli, "load_artifact_schema_graph", lambda root: _SchemaGraph())
    args = SimpleNamespace(schema_action=action, artifact="requirements", json=True)

    assert schema_cli.run_schema_command(Path("."), args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apiVersion"] == expected
    if action != "show":
        assert payload["version"] == 1


def test_technology_json_is_machine_clean_and_versioned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _SimpleReport()
    monkeypatch.setattr(tech_cli, "detect_technologies", lambda root: report)
    args = SimpleNamespace(tech_action="detect", json=True)

    assert tech_cli.run_tech_command(Path("."), args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "apiVersion": tech_cli.TECHNOLOGY_REPORT_API_VERSION,
        "status": "ok",
        "version": 1,
    }


def test_skill_resolution_json_is_machine_clean_and_versioned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(skill_cli, "resolve_skills", lambda *args, **kwargs: _SimpleReport())
    args = SimpleNamespace(
        agent="developer",
        capability="coding",
        task=None,
        domain=None,
        skills=[],
        json=True,
    )

    assert skill_cli.run_skill_resolution_command(Path("."), args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apiVersion"] == skill_cli.SKILL_RESOLUTION_API_VERSION
    assert payload["version"] == 1


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("validate", spec_cli.SPEC_VALIDATION_API_VERSION),
        ("diff", spec_cli.SPEC_DIFF_API_VERSION),
        ("approve", spec_cli.SPEC_PROMOTION_APPROVAL_API_VERSION),
        ("preview", spec_cli.SPEC_PROMOTION_PREVIEW_API_VERSION),
        ("promote", spec_cli.SPEC_PROMOTION_RESULT_API_VERSION),
    ],
)
def test_spec_json_is_machine_clean_and_versioned(
    action: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(spec_cli, "validate_spec_change", lambda *args: _ValidationReport())
    monkeypatch.setattr(spec_cli, "build_spec_diff", lambda *args: _SimpleReport())
    monkeypatch.setattr(spec_cli, "record_promotion_approval", lambda *args, **kwargs: _ApprovalDecision())
    monkeypatch.setattr(spec_cli, "preview_promotion", lambda *args: _PromotionPreview())
    monkeypatch.setattr(spec_cli, "promote_spec_change", lambda *args: _SimpleReport())
    if action == "preview":
        spec_action = "promote"
        dry_run = True
    else:
        spec_action = action
        dry_run = False
    args = SimpleNamespace(
        spec_action=spec_action,
        feature="JSON-100",
        json=True,
        include_content=False,
        approved_by="reviewer",
        role="",
        note="",
        dry_run=dry_run,
    )

    assert spec_cli.run_spec_command(Path("."), args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apiVersion"] == expected
    assert payload["version"] == 1


def test_behavioral_eval_serializer_adds_api_version_without_dropping_legacy_version() -> None:
    report = EvalReport(
        target_type="skill",
        target_name="example",
        target_sha256="0" * 64,
        provider="mock",
        model="deterministic-v1",
        scenarios=(),
        baseline_score=0.0,
        candidate_score=0.0,
        delta=0.0,
        required_failures=(),
        regressions=(),
        require_improvement=False,
        improvement_satisfied=True,
        passed=True,
    )

    payload = json.loads(report.to_json())
    assert payload["apiVersion"] == EVAL_REPORT_API_VERSION
    assert payload["version"] == 1


def test_legacy_workflow_definition_payload_is_versioned_additively() -> None:
    definition = SimpleNamespace(
        input_definitions=(),
        input_values={},
        name="legacy",
        workflow_version=8,
        validation_mode=SimpleNamespace(value="strict"),
        components=(),
        inheritance=(),
        overlays=(),
        lifecycle_hooks=(),
        mandatory_steps=(),
        iter_steps=lambda: iter(()),
    )

    payload = _definition_payload(definition, {})
    assert payload["apiVersion"] == WORKFLOW_LEGACY_DEFINITION_API_VERSION
    assert payload["version"] == 1
    assert payload["workflow_version"] == 8
