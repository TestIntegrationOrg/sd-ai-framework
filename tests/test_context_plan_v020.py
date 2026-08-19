from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.agent_platform import (
    CONTEXT_PLAN_API_VERSION,
    CONTEXT_PLAN_MAX_FILES,
    AgentRuntime,
    Capability,
    ContextPlanError,
    ExecutionMode,
    build_context_plan,
)
from sdai.scaffold import init_project


FEATURE = "CONTEXT-020"
SECRET = "do-not-leak-context-plan-secret"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _current_workspace(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "00-intake.md",
        f"# Feature Intake — {FEATURE}\n\n## Title\nContext planning\n\n## Description\nPlan only relevant context.\n",
    )
    _write(
        feature / "requirements.md",
        "# Requirements\n\n- FR-020: Context selection MUST be deterministic.\n",
    )
    _write(
        feature / "architecture" / "architecture.md",
        "# Architecture\n\nADR-020 drives the context planner.\n",
    )
    _write(
        feature / "adr" / "ADR-020-context.md",
        "# ADR-020: Deterministic context planning\n\nUse hash-bound context plans.\n",
    )
    _write(
        feature / "tasks.yaml",
        "tasks:\n  - id: TASK-020\n    title: Implement context planning\n    status: ready\n",
    )
    _write(
        feature / "evidence" / "test-result.json",
        '{"apiVersion":"example/v1","status":"passed"}\n',
    )
    _write(feature / "notes" / "unrelated.md", f"unrelated {SECRET}\n")
    _write(
        root / "src" / "context_worker.py",
        "# FR-020\n\ndef build_context() -> None:\n    pass\n",
    )
    _write(root / "src" / "unrelated.py", f"# unrelated source marker: {SECRET}\n")
    return feature


def _legacy_workspace(root: Path) -> Path:
    feature = root / "specs" / FEATURE
    _write(
        feature / "00-intake.md",
        f"# Feature Intake — {FEATURE}\n\n## Title\nLegacy context\n",
    )
    _write(feature / "specification.md", "# Specification\n\nLegacy feature workspace.\n")
    _write(feature / "plan.md", "# Plan\n\nLegacy implementation plan.\n")
    return feature


def test_current_context_plan_is_deterministic_trace_aware_and_secret_safe(tmp_path: Path) -> None:
    init_project(tmp_path)
    feature = _current_workspace(tmp_path)

    first = build_context_plan(
        tmp_path,
        FEATURE,
        Capability.CODING,
        max_chars_per_file=30_000,
    )
    second = build_context_plan(
        tmp_path,
        FEATURE,
        Capability.CODING,
        max_chars_per_file=30_000,
    )

    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    payload = json.loads(first.to_json())
    assert payload["apiVersion"] == CONTEXT_PLAN_API_VERSION
    assert payload["planSha256"] == first.sha256
    assert payload["workspace"] == f"specs/changes/{FEATURE}"

    sources = {item.source: item for item in first.files}
    assert f"specs/changes/{FEATURE}/requirements.md" in sources
    assert f"specs/changes/{FEATURE}/tasks.yaml" in sources
    source_item = sources["src/context_worker.py"]
    assert "trace-source-reference:FR-020" in source_item.reasons
    assert "src/unrelated.py" not in sources
    assert f"specs/changes/{FEATURE}/notes/unrelated.md" not in sources

    # The plan is metadata only. Raw selected or unselected contents never become
    # part of its stable JSON contract.
    assert SECRET not in first.to_json()
    rendered = first.render_feature_context(tmp_path)
    assert "FR-020" in rendered
    assert "TASK-020" in rendered
    assert "def build_context" in rendered
    assert SECRET not in rendered
    assert feature.resolve() == (tmp_path / first.workspace).resolve()


def test_runtime_context_plan_selects_only_capability_applicable_skills(tmp_path: Path) -> None:
    init_project(tmp_path)
    _current_workspace(tmp_path)

    runtime = AgentRuntime(tmp_path)
    plan = runtime.build_context_plan(FEATURE, Capability.ARCHITECTURE)
    selected = set(plan.selected_skill_names)

    assert "spec-traceability" in selected
    assert "architecture-review" in selected
    assert "secure-coding" not in selected
    assert any(
        item.name == "secure-coding"
        and item.selected is False
        and item.exclusion_reason == "capability-not-applicable"
        for item in plan.skills
    )

    invocation = runtime.build_invocation(FEATURE, Capability.ARCHITECTURE)
    assert "Skill: spec-traceability" in invocation.system
    assert "Skill: architecture-review" in invocation.system
    assert "Skill: secure-coding" not in invocation.system


def test_context_plan_detects_selected_artifact_mutation_before_render(tmp_path: Path) -> None:
    init_project(tmp_path)
    feature = _current_workspace(tmp_path)
    plan = build_context_plan(
        tmp_path,
        FEATURE,
        Capability.CODING,
        max_chars_per_file=30_000,
    )

    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-020: mutated after planning.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContextPlanError, match="changed after planning"):
        plan.render_feature_context(tmp_path)


def test_context_plan_detects_skill_manifest_applicability_mutation(tmp_path: Path) -> None:
    init_project(tmp_path)
    _current_workspace(tmp_path)
    plan = AgentRuntime(tmp_path).build_context_plan(FEATURE, Capability.ARCHITECTURE)
    assert "architecture-review" in plan.selected_skill_names

    manifest = tmp_path / ".sdai" / "skills" / "architecture-review" / "skill.yaml"
    manifest.write_text(
        "name: architecture-review\n"
        "description: Generate and evaluate architecture options using explicit quality attributes and ADRs.\n"
        "capabilities: [review]\n",
        encoding="utf-8",
    )

    with pytest.raises(ContextPlanError, match="planned skill changed after planning"):
        plan.render_skills(tmp_path)


def test_legacy_feature_workspace_uses_deterministic_fallback_without_current_index(tmp_path: Path) -> None:
    init_project(tmp_path)
    legacy = _legacy_workspace(tmp_path)

    plan = build_context_plan(
        tmp_path,
        FEATURE,
        Capability.CODING,
        max_chars_per_file=30_000,
    )

    assert plan.workspace == f"specs/{FEATURE}"
    assert "legacy-workspace-trace-fallback" in plan.diagnostics
    sources = {item.source for item in plan.files}
    assert f"specs/{FEATURE}/00-intake.md" in sources
    assert f"specs/{FEATURE}/specification.md" in sources
    assert f"specs/{FEATURE}/plan.md" in sources
    assert "Legacy implementation plan" in plan.render_feature_context(tmp_path)
    assert legacy.resolve() == (tmp_path / plan.workspace).resolve()


def test_explicit_context_invocation_does_not_scan_feature_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_project(tmp_path)
    _current_workspace(tmp_path)
    runtime = AgentRuntime(tmp_path)

    import sdai.agent_platform.runtime as runtime_module

    def fail_if_planned(*args: object, **kwargs: object) -> object:
        raise AssertionError("explicit context path must not build/scan a feature context plan")

    monkeypatch.setattr(runtime_module, "plan_context", fail_if_planned)
    invocation = runtime.build_explicit_context_invocation(
        FEATURE,
        Capability.CODING,
        "ONLY-THIS-EXPLICIT-CONTEXT café Δ",
        mode=ExecutionMode.ADVISORY,
    )

    assert "ONLY-THIS-EXPLICIT-CONTEXT café Δ" in invocation.prompt
    assert SECRET not in invocation.prompt


def test_context_plan_truncation_is_bounded_and_explained(tmp_path: Path) -> None:
    init_project(tmp_path)
    feature = _current_workspace(tmp_path)
    long_text = "x" * 1500
    _write(feature / "implementation-brief.md", long_text)

    plan = build_context_plan(
        tmp_path,
        FEATURE,
        Capability.CODING,
        max_chars_per_file=1000,
    )
    item = next(
        entry
        for entry in plan.files
        if entry.source.endswith("implementation-brief.md")
    )

    assert item.truncated is True
    assert item.chars == 1500
    assert item.selected_chars == 1000
    rendered = plan.render_feature_context(tmp_path)
    assert "[truncated by SD-AI]" in rendered
    assert "x" * 1001 not in rendered


def test_governance_authority_is_not_displaced_by_feature_file_budget(tmp_path: Path) -> None:
    init_project(tmp_path)
    feature = _current_workspace(tmp_path)
    for index in range(CONTEXT_PLAN_MAX_FILES + 20):
        _write(feature / "evidence" / f"result-{index:03d}.md", f"result {index}\n")

    plan = build_context_plan(
        tmp_path,
        FEATURE,
        Capability.CODING,
        max_chars_per_file=30_000,
    )
    sources = {item.source for item in plan.files}

    assert len(plan.files) == CONTEXT_PLAN_MAX_FILES
    assert ".sdai/constitution.yaml" in sources
    assert ".sdai/policies.yaml" in sources
    assert any(item.reason == "file-budget-exceeded" for item in plan.exclusions)
