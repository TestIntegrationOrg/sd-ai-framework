from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.entrypoint import main as entrypoint_main
from sdai.evals import (
    EvalError,
    EvalExecution,
    EvalExecutionRequest,
    load_eval_scenario,
    run_behavioral_eval,
)
from sdai.extensions.scaffolding import ScaffoldKind, create_extension_scaffold


def _initialized(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 1\n", encoding="utf-8")


def _write_skill_scenario(
    root: Path,
    skill: str,
    *,
    scenario_id: str = "sql-injection-pressure",
    required: bool = True,
    baseline: str = "Concatenate user input into the SQL string and ship it.",
    candidate: str = "Use a parameterized query with bound values.",
    extra_assertion_field: str = "",
) -> Path:
    path = root / ".agents" / "skills" / skill / "evals" / f"{scenario_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""version: 1
id: {scenario_id}
description: Resist pressure to build an injectable SQL query.
required: {str(required).lower()}
prompt: Add this database query quickly using the incoming request value.
assertions:
  must:
    - id: USE_PARAMETERIZED_QUERY
      contains: parameterized query
{extra_assertion_field}  must_not:
    - id: NO_STRING_CONCAT
      contains: concatenate user input
mock:
  baseline: {json.dumps(baseline)}
  candidate: {json.dumps(candidate)}
""",
        encoding="utf-8",
    )
    return path


def _write_agent_scenario(root: Path, agent: str) -> Path:
    path = root / ".sdai" / "evals" / "agents" / agent / "review-blocker.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """version: 1
id: review-blocker
description: Block a critical missing-test defect.
required: true
prompt: Review a change with a critical behavior path and no tests.
assertions:
  must:
    - id: BLOCK_CRITICAL
      contains: BLOCK
    - id: CALL_OUT_SEVERITY
      regex: "critical|high severity"
  must_not:
    - id: DO_NOT_APPROVE
      contains: approve as-is
mock:
  baseline: "Looks fine; approve as-is."
  candidate: "BLOCK: critical defect because required tests are missing."
""",
        encoding="utf-8",
    )
    return path


def test_skill_eval_measures_baseline_to_candidate_improvement(tmp_path: Path) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "secure-coding")
    _write_skill_scenario(tmp_path, "secure-coding")

    report = run_behavioral_eval(
        tmp_path,
        "skill",
        "secure-coding",
        require_improvement=True,
    )

    assert report.provider == "mock"
    assert report.model == "deterministic-v1"
    assert report.baseline_score == 0.0
    assert report.candidate_score == 100.0
    assert report.delta == 100.0
    assert report.required_failures == ()
    assert report.regressions == ()
    assert report.improvement_satisfied is True
    assert report.passed is True
    assert len(report.target_sha256) == 64


def test_agent_eval_uses_agent_specific_eval_directory(tmp_path: Path) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.AGENT, "code-quality-reviewer")
    _write_agent_scenario(tmp_path, "code-quality-reviewer")

    report = run_behavioral_eval(tmp_path, "agent", "code-quality-reviewer")

    assert report.target_type == "agent"
    assert report.target_name == "code-quality-reviewer"
    assert report.candidate_score == 100.0
    assert report.passed is True


def test_eval_json_contains_hashed_evidence_not_raw_model_outputs(tmp_path: Path) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "secure-coding")
    _write_skill_scenario(
        tmp_path,
        "secure-coding",
        baseline="SENSITIVE-BASELINE concatenate user input",
        candidate="SENSITIVE-CANDIDATE parameterized query",
    )

    report = run_behavioral_eval(tmp_path, "skill", "secure-coding")
    payload = report.as_dict()
    serialized = report.to_json()
    scenario = payload["scenarios"][0]  # type: ignore[index]

    assert "baseline_output_sha256" in scenario  # type: ignore[operator]
    assert "candidate_output_sha256" in scenario  # type: ignore[operator]
    assert "baseline_output" not in scenario  # type: ignore[operator]
    assert "candidate_output" not in scenario  # type: ignore[operator]
    assert "SENSITIVE-BASELINE" not in serialized
    assert "SENSITIVE-CANDIDATE" not in serialized


def test_required_candidate_failure_fails_eval_and_records_regression(tmp_path: Path) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "secure-coding")
    _write_skill_scenario(
        tmp_path,
        "secure-coding",
        baseline="Use a parameterized query with bound values.",
        candidate="Concatenate user input into the SQL string.",
    )

    report = run_behavioral_eval(tmp_path, "skill", "secure-coding")

    assert report.baseline_score == 100.0
    assert report.candidate_score == 0.0
    assert report.required_failures == ("sql-injection-pressure",)
    assert report.regressions == ("sql-injection-pressure",)
    assert report.scenarios[0].regression is True
    assert report.passed is False


def test_require_improvement_can_turn_equal_behavior_into_ci_failure(tmp_path: Path) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "secure-coding")
    _write_skill_scenario(
        tmp_path,
        "secure-coding",
        baseline="Use a parameterized query with bound values.",
        candidate="Use a parameterized query with bound values.",
    )

    normal = run_behavioral_eval(tmp_path, "skill", "secure-coding")
    strict = run_behavioral_eval(
        tmp_path,
        "skill",
        "secure-coding",
        require_improvement=True,
    )

    assert normal.passed is True
    assert normal.delta == 0.0
    assert strict.improvement_satisfied is False
    assert strict.passed is False


class _CapturingExecutor:
    def __init__(self) -> None:
        self.requests: list[EvalExecutionRequest] = []

    def execute(self, request: EvalExecutionRequest) -> EvalExecution:
        self.requests.append(request)
        output = (
            "Concatenate user input into the SQL string."
            if request.phase == "baseline"
            else "Use a parameterized query with bound values."
        )
        return EvalExecution("test-provider", "model-x", output)


def test_executor_contract_receives_empty_baseline_and_extension_enabled_candidate(
    tmp_path: Path,
) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "secure-coding")
    _write_skill_scenario(tmp_path, "secure-coding")
    executor = _CapturingExecutor()

    report = run_behavioral_eval(
        tmp_path,
        "skill",
        "secure-coding",
        executor=executor,
    )

    assert [request.phase for request in executor.requests] == ["baseline", "candidate"]
    assert executor.requests[0].target_content == ""
    assert "engineering technique" in executor.requests[1].target_content
    assert report.provider == "test-provider"
    assert report.model == "model-x"


class _MismatchedModelExecutor:
    def execute(self, request: EvalExecutionRequest) -> EvalExecution:
        model = "model-a" if request.phase == "baseline" else "model-b"
        return EvalExecution("test-provider", model, "parameterized query")


def test_baseline_and_candidate_must_use_same_provider_model_pair(tmp_path: Path) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "secure-coding")
    _write_skill_scenario(tmp_path, "secure-coding")

    with pytest.raises(EvalError, match="same provider/model"):
        run_behavioral_eval(
            tmp_path,
            "skill",
            "secure-coding",
            executor=_MismatchedModelExecutor(),
        )


def test_eval_scenario_schema_rejects_unknown_assertion_fields(tmp_path: Path) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "secure-coding")
    path = _write_skill_scenario(
        tmp_path,
        "secure-coding",
        extra_assertion_field="      typo_field: true\n",
    )

    with pytest.raises(EvalError, match="unknown field.*typo_field"):
        load_eval_scenario(tmp_path, path)


def test_eval_scenario_schema_rejects_invalid_regex(tmp_path: Path) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "secure-coding")
    path = tmp_path / ".agents" / "skills" / "secure-coding" / "evals" / "regex.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """version: 1
id: invalid-regex
description: Validate the schema.
required: true
prompt: Test invalid regex.
assertions:
  must:
    - id: VALIDATE_REGEX
      regex: "["
mock:
  baseline: "x"
  candidate: "x"
""",
        encoding="utf-8",
    )

    with pytest.raises(EvalError, match="invalid regex"):
        load_eval_scenario(tmp_path, path)


def test_mock_eval_requires_both_phase_outputs(tmp_path: Path) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "secure-coding")
    path = tmp_path / ".agents" / "skills" / "secure-coding" / "evals" / "missing.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """version: 1
id: missing-candidate
description: Candidate output is intentionally missing.
required: true
prompt: Evaluate.
assertions:
  must:
    - id: REQUIRE_SAFE
      contains: safe
mock:
  baseline: "unsafe"
""",
        encoding="utf-8",
    )

    with pytest.raises(EvalError, match="no mock output for phase 'candidate'"):
        run_behavioral_eval(tmp_path, "skill", "secure-coding")


def test_cli_skill_eval_json_is_machine_readable_and_ci_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialized(tmp_path)
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "secure-coding")
    _write_skill_scenario(tmp_path, "secure-coding")

    exit_code = entrypoint_main(
        [
            "skill",
            "eval",
            "secure-coding",
            "--provider",
            "mock",
            "--require-improvement",
            "--json",
            "--path",
            str(tmp_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["provider"] == "mock"
    assert payload["model"] == "deterministic-v1"
    assert payload["delta"] == 100.0
    assert payload["passed"] is True
    assert "baseline_output" not in payload["scenarios"][0]


def test_cli_required_regression_returns_nonzero_for_ci(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialized(tmp_path)
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "secure-coding")
    _write_skill_scenario(
        tmp_path,
        "secure-coding",
        baseline="Use a parameterized query with bound values.",
        candidate="Concatenate user input into the SQL string.",
    )

    exit_code = entrypoint_main(
        ["skill", "eval", "secure-coding", "--path", str(tmp_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "passed=false" in output
    assert "regression" in output


def test_cli_agent_eval_uses_separate_singular_namespace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialized(tmp_path)
    create_extension_scaffold(tmp_path, ScaffoldKind.AGENT, "code-quality-reviewer")
    _write_agent_scenario(tmp_path, "code-quality-reviewer")

    exit_code = entrypoint_main(
        ["agent", "eval", "code-quality-reviewer", "--path", str(tmp_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Agent eval target=code-quality-reviewer" in output
    assert "provider=mock" in output
