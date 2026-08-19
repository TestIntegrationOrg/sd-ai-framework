from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.agent_platform import AgentRuntime, Capability, ContextPlanError
from sdai.context_explain import (
    CONTEXT_EXPLAIN_API_VERSION,
    build_context_explanation,
)
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project
from sdai.version_entrypoint import main as sdai_main


FEATURE = "CONTEXT-EXPLAIN-020"
SECRET = "selected-context-secret-that-must-never-be-emitted"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _current(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        f"# Requirements\n\n- FR-252: Explain context metrics safely. Marker={SECRET}\n",
    )
    _write(
        feature / "architecture" / "architecture.md",
        "# Architecture\n\nADR-252 defines deterministic explanation.\n",
    )
    _write(
        feature / "adr" / "ADR-252-context.md",
        "# ADR-252: Context explanation\n\nExpose only metadata, hashes and sizes.\n",
    )
    _write(
        root / "src" / "context_explain_worker.py",
        "# FR-252\n\ndef explain() -> None:\n    pass\n",
    )
    return feature


def _legacy(root: Path) -> Path:
    feature = root / "specs" / FEATURE
    _write(
        feature / "00-intake.md",
        f"# Feature Intake — {FEATURE}\n\n## Title\nLegacy explain\n",
    )
    _write(feature / "specification.md", "# Specification\n\nLegacy context explain.\n")
    return feature


def test_context_explain_json_is_deterministic_private_and_provider_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_project(tmp_path)
    _current(tmp_path)

    def provider_must_not_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("context explain must never create a provider")

    monkeypatch.setattr(ProviderFactory, "create", staticmethod(provider_must_not_run))
    argv = [
        "context",
        "explain",
        FEATURE,
        "--capability",
        "coding",
        "--json",
        "--path",
        str(tmp_path),
    ]
    assert sdai_main(argv) == 0
    first_text = capsys.readouterr().out
    assert sdai_main(argv) == 0
    second_text = capsys.readouterr().out

    assert first_text == second_text
    assert SECRET not in first_text
    body = json.loads(first_text)
    assert body["apiVersion"] == CONTEXT_EXPLAIN_API_VERSION
    assert body["featureId"] == FEATURE
    assert body["capability"] == "coding"
    assert body["workspace"] == f"specs/changes/{FEATURE}"
    assert body["contextPlan"]["planSha256"].startswith("sha256:")
    assert body["reportSha256"].startswith("sha256:")
    assert body["tokenEstimate"] == {
        "available": False,
        "components": {},
        "reason": "provider-tokenizer-not-configured",
    }

    metrics = body["metrics"]
    for name in (
        "combinedPrompt",
        "featureContext",
        "governanceContext",
        "skillsContext",
        "systemPrompt",
        "taskPrompt",
    ):
        assert metrics[name]["chars"] >= 0
        assert metrics[name]["utf8Bytes"] >= metrics[name]["chars"]
        assert metrics[name]["sha256"].startswith("sha256:")
    assert metrics["combinedPrompt"]["chars"] > metrics["taskPrompt"]["chars"]

    selected = body["contextPlan"]["files"]
    assert any(item["source"].endswith("requirements.md") for item in selected)
    assert any(item["source"] == "src/context_explain_worker.py" for item in selected)
    assert all("reasons" in item and item["sha256"].startswith("sha256:") for item in selected)


def test_context_explain_human_output_is_bounded_metadata_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_project(tmp_path)
    _current(tmp_path)

    assert sdai_main(
        [
            "context",
            "explain",
            FEATURE,
            "--capability",
            "review",
            "--path",
            str(tmp_path),
        ]
    ) == 0
    output = capsys.readouterr().out

    assert f"Context {FEATURE} capability=review" in output
    assert "plan_sha256=sha256:" in output
    assert "combinedPrompt: chars=" in output
    assert "selected_context=" in output
    assert "reasons=" in output
    assert SECRET not in output


def test_context_explain_invalid_capability_has_stable_json_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_project(tmp_path)
    _current(tmp_path)

    assert sdai_main(
        [
            "context",
            "explain",
            FEATURE,
            "--capability",
            "not-a-capability",
            "--json",
            "--path",
            str(tmp_path),
        ]
    ) == 4
    body = json.loads(capsys.readouterr().out)
    assert body["apiVersion"] == "sdai.context-explain-error/v1"
    assert body["category"] == "input"
    assert body["error"]["code"] == "SDAI-CONTEXT-CLI-001"
    assert body["errorSha256"].startswith("sha256:")


def test_explanation_supports_optional_deterministic_token_estimator(tmp_path: Path) -> None:
    init_project(tmp_path)
    _current(tmp_path)

    explanation = build_context_explanation(
        tmp_path,
        FEATURE,
        Capability.CODING,
        token_estimator=lambda text: len(text.encode("utf-8")) // 4,
    )
    token = explanation.token_estimate

    assert token.available is True
    assert set(token.values) == set(explanation.metrics)
    assert all(value >= 0 for value in token.values.values())
    exported = explanation.to_json()
    assert SECRET not in exported
    assert json.loads(exported)["tokenEstimate"]["available"] is True


def test_invocation_from_explained_plan_fails_closed_after_context_drift(tmp_path: Path) -> None:
    init_project(tmp_path)
    feature = _current(tmp_path)
    runtime = AgentRuntime(tmp_path)
    plan = runtime.build_context_plan(FEATURE, Capability.CODING)

    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-252: changed after explanation.\n",
        encoding="utf-8",
    )

    with pytest.raises(ContextPlanError, match="stale or no longer canonical"):
        runtime.build_invocation_from_context_plan(plan)


def test_context_explain_reports_legacy_workspace_without_provider(tmp_path: Path) -> None:
    init_project(tmp_path)
    _legacy(tmp_path)

    explanation = build_context_explanation(
        tmp_path,
        FEATURE,
        Capability.DOCUMENTATION,
    )

    assert explanation.plan.workspace == f"specs/{FEATURE}"
    assert "legacy-workspace-trace-fallback" in explanation.plan.diagnostics
    assert explanation.metrics["featureContext"].chars > 0
    assert explanation.profile
    assert explanation.provider
