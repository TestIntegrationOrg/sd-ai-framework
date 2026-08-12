from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Protocol

import yaml

from sdai.agent_platform.definitions import load_agent_definition
from sdai.agent_platform.skills import load_skill
from sdai.path_safety import ensure_within_project
from sdai.text import read_utf8_text


class EvalError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvalAssertion:
    id: str
    mode: str
    pattern: str
    regex: bool = False
    case_sensitive: bool = False


@dataclass(frozen=True)
class EvalScenario:
    id: str
    description: str
    required: bool
    prompt: str
    assertions: tuple[EvalAssertion, ...]
    mock_baseline: str | None
    mock_candidate: str | None
    path: Path
    sha256: str


@dataclass(frozen=True)
class EvalExecutionRequest:
    target_type: str
    target_name: str
    phase: str
    prompt: str
    target_content: str
    scenario: EvalScenario


@dataclass(frozen=True)
class EvalExecution:
    provider: str
    model: str
    output: str


class EvalExecutor(Protocol):
    def execute(self, request: EvalExecutionRequest) -> EvalExecution: ...


@dataclass(frozen=True)
class AssertionResult:
    id: str
    mode: str
    pattern: str
    passed: bool


@dataclass(frozen=True)
class ScenarioResult:
    id: str
    description: str
    required: bool
    scenario_sha256: str
    baseline_output_sha256: str
    candidate_output_sha256: str
    baseline_assertions: tuple[AssertionResult, ...]
    candidate_assertions: tuple[AssertionResult, ...]
    baseline_score: float
    candidate_score: float
    delta: float
    passed: bool
    regression: bool


@dataclass(frozen=True)
class EvalReport:
    target_type: str
    target_name: str
    target_sha256: str
    provider: str
    model: str
    scenarios: tuple[ScenarioResult, ...]
    baseline_score: float
    candidate_score: float
    delta: float
    required_failures: tuple[str, ...]
    regressions: tuple[str, ...]
    require_improvement: bool
    improvement_satisfied: bool
    passed: bool

    def as_dict(self) -> dict[str, object]:
        """Return CI-safe evidence without embedding raw model responses."""

        return {
            "version": 1,
            "target_type": self.target_type,
            "target_name": self.target_name,
            "target_sha256": self.target_sha256,
            "provider": self.provider,
            "model": self.model,
            "baseline_score": self.baseline_score,
            "candidate_score": self.candidate_score,
            "delta": self.delta,
            "required_failures": list(self.required_failures),
            "regressions": list(self.regressions),
            "require_improvement": self.require_improvement,
            "improvement_satisfied": self.improvement_satisfied,
            "passed": self.passed,
            "scenarios": [
                {
                    "id": result.id,
                    "description": result.description,
                    "required": result.required,
                    "scenario_sha256": result.scenario_sha256,
                    "baseline_output_sha256": result.baseline_output_sha256,
                    "candidate_output_sha256": result.candidate_output_sha256,
                    "baseline_score": result.baseline_score,
                    "candidate_score": result.candidate_score,
                    "delta": result.delta,
                    "passed": result.passed,
                    "regression": result.regression,
                    "baseline_assertions": [
                        {
                            "id": assertion.id,
                            "mode": assertion.mode,
                            "pattern": assertion.pattern,
                            "passed": assertion.passed,
                        }
                        for assertion in result.baseline_assertions
                    ],
                    "candidate_assertions": [
                        {
                            "id": assertion.id,
                            "mode": assertion.mode,
                            "pattern": assertion.pattern,
                            "passed": assertion.passed,
                        }
                        for assertion in result.candidate_assertions
                    ],
                }
                for result in self.scenarios
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


_ASSERTION_ID = re.compile(r"^[A-Z][A-Z0-9_-]{2,63}$")
_SCENARIO_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ASSERTION_KEYS = frozenset({"id", "contains", "regex", "case_sensitive"})
_SCENARIO_KEYS = frozenset(
    {"version", "id", "description", "required", "prompt", "assertions", "mock"}
)
_MOCK_KEYS = frozenset({"baseline", "candidate"})


class MockEvalExecutor:
    """Deterministic executor for CI and extension-author feedback loops."""

    provider = "mock"
    model = "deterministic-v1"

    def execute(self, request: EvalExecutionRequest) -> EvalExecution:
        output = (
            request.scenario.mock_baseline
            if request.phase == "baseline"
            else request.scenario.mock_candidate
        )
        if output is None:
            raise EvalError(
                f"Scenario '{request.scenario.id}' has no mock output for phase "
                f"'{request.phase}'"
            )
        return EvalExecution(self.provider, self.model, output)


def _scenario_root(project_root: Path, target_type: str, target_name: str) -> Path:
    root = project_root.resolve()
    if target_type == "skill":
        candidate = root / ".agents" / "skills" / target_name / "evals"
        return ensure_within_project(root, candidate, label="skill eval directory")
    if target_type == "agent":
        candidate = root / ".sdai" / "evals" / "agents" / target_name
        return ensure_within_project(root, candidate, label="agent eval directory")
    raise EvalError(f"Unsupported eval target type '{target_type}'")


def _target_content(project_root: Path, target_type: str, target_name: str) -> str:
    if target_type == "skill":
        skill = load_skill(project_root, target_name)
        return skill.instructions
    if target_type == "agent":
        agent = load_agent_definition(project_root, target_name)
        return agent.instructions
    raise EvalError(f"Unsupported eval target type '{target_type}'")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalError(f"{label} must be a non-empty string")
    return value.strip()


def _unknown_keys(raw: dict[object, object], allowed: frozenset[str]) -> list[str]:
    return sorted(str(key) for key in raw if key not in allowed)


def _load_assertions(raw: object, scenario_id: str) -> tuple[EvalAssertion, ...]:
    if not isinstance(raw, dict):
        raise EvalError(f"Scenario '{scenario_id}' assertions must be a mapping")
    unknown_modes = sorted(set(raw) - {"must", "must_not"})
    if unknown_modes:
        raise EvalError(
            f"Scenario '{scenario_id}' assertions contains unknown field(s): "
            f"{', '.join(str(value) for value in unknown_modes)}"
        )
    assertions: list[EvalAssertion] = []
    seen: set[str] = set()
    for mode in ("must", "must_not"):
        values = raw.get(mode, [])
        if not isinstance(values, list):
            raise EvalError(
                f"Scenario '{scenario_id}' assertions.{mode} must be a list"
            )
        for index, item in enumerate(values, start=1):
            if not isinstance(item, dict):
                raise EvalError(
                    f"Scenario '{scenario_id}' {mode} assertion #{index} must be a mapping"
                )
            unknown = _unknown_keys(item, _ASSERTION_KEYS)
            if unknown:
                raise EvalError(
                    f"Scenario '{scenario_id}' {mode} assertion #{index} contains "
                    f"unknown field(s): {', '.join(unknown)}"
                )
            assertion_id = _string(
                item.get("id"),
                f"{scenario_id}.{mode}[{index}].id",
            )
            if not _ASSERTION_ID.fullmatch(assertion_id):
                raise EvalError(
                    f"Assertion id '{assertion_id}' must use uppercase letters, numbers, "
                    "underscore, or hyphen and contain 3-64 characters"
                )
            if assertion_id in seen:
                raise EvalError(
                    f"Scenario '{scenario_id}' contains duplicate assertion id "
                    f"'{assertion_id}'"
                )
            seen.add(assertion_id)
            contains = item.get("contains")
            regex_pattern = item.get("regex")
            if (contains is None) == (regex_pattern is None):
                raise EvalError(
                    f"Assertion '{assertion_id}' must define exactly one of contains or regex"
                )
            pattern = _string(
                contains if contains is not None else regex_pattern,
                f"{assertion_id}.pattern",
            )
            if regex_pattern is not None:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise EvalError(
                        f"Assertion '{assertion_id}' has invalid regex: {exc}"
                    ) from exc
            case_sensitive = item.get("case_sensitive", False)
            if not isinstance(case_sensitive, bool):
                raise EvalError(
                    f"Assertion '{assertion_id}' case_sensitive must be a boolean"
                )
            assertions.append(
                EvalAssertion(
                    id=assertion_id,
                    mode=mode,
                    pattern=pattern,
                    regex=regex_pattern is not None,
                    case_sensitive=case_sensitive,
                )
            )
    if not assertions:
        raise EvalError(f"Scenario '{scenario_id}' must define at least one assertion")
    return tuple(assertions)


def load_eval_scenario(project_root: Path, path: Path) -> EvalScenario:
    root = project_root.resolve()
    candidate = path if path.is_absolute() else root / path
    safe = ensure_within_project(root, candidate, label="eval scenario path")
    if not safe.is_file():
        raise EvalError(f"Eval scenario does not exist or is not a file: {safe}")
    text = read_utf8_text(safe)
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise EvalError(f"Invalid eval YAML in '{safe}': {exc}") from exc
    if not isinstance(raw, dict):
        raise EvalError(f"Eval scenario '{safe}' must be a mapping")
    unknown = _unknown_keys(raw, _SCENARIO_KEYS)
    if unknown:
        raise EvalError(
            f"Eval scenario '{safe}' contains unknown field(s): {', '.join(unknown)}"
        )
    if raw.get("version") != 1:
        raise EvalError(f"Eval scenario '{safe}' version must be 1")
    scenario_id = _string(raw.get("id"), "scenario.id")
    if not _SCENARIO_ID.fullmatch(scenario_id):
        raise EvalError(
            f"Scenario id '{scenario_id}' must use lowercase portable identifier syntax"
        )
    description = _string(raw.get("description"), f"{scenario_id}.description")
    prompt = _string(raw.get("prompt"), f"{scenario_id}.prompt")
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise EvalError(f"Scenario '{scenario_id}' required must be a boolean")
    assertions = _load_assertions(raw.get("assertions"), scenario_id)
    mock = raw.get("mock", {})
    if not isinstance(mock, dict):
        raise EvalError(f"Scenario '{scenario_id}' mock must be a mapping")
    mock_unknown = _unknown_keys(mock, _MOCK_KEYS)
    if mock_unknown:
        raise EvalError(
            f"Scenario '{scenario_id}' mock contains unknown field(s): "
            f"{', '.join(mock_unknown)}"
        )
    mock_baseline = mock.get("baseline")
    mock_candidate = mock.get("candidate")
    if mock_baseline is not None and not isinstance(mock_baseline, str):
        raise EvalError(f"Scenario '{scenario_id}' mock.baseline must be a string")
    if mock_candidate is not None and not isinstance(mock_candidate, str):
        raise EvalError(f"Scenario '{scenario_id}' mock.candidate must be a string")
    return EvalScenario(
        id=scenario_id,
        description=description,
        required=required,
        prompt=prompt,
        assertions=assertions,
        mock_baseline=mock_baseline,
        mock_candidate=mock_candidate,
        path=safe,
        sha256=sha256(text.encode("utf-8")).hexdigest(),
    )


def load_eval_scenarios(
    project_root: Path,
    target_type: str,
    target_name: str,
) -> tuple[EvalScenario, ...]:
    root = _scenario_root(project_root, target_type, target_name)
    if not root.exists():
        raise EvalError(
            f"No eval directory for {target_type} '{target_name}': {root}"
        )
    paths = sorted(
        [*root.glob("*.yaml"), *root.glob("*.yml")],
        key=lambda path: path.name.casefold(),
    )
    if not paths:
        raise EvalError(
            f"No eval scenarios found for {target_type} '{target_name}'"
        )
    scenarios = tuple(load_eval_scenario(project_root, path) for path in paths)
    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.id in seen:
            raise EvalError(
                f"Duplicate eval scenario id '{scenario.id}' for "
                f"{target_type} '{target_name}'"
            )
        seen.add(scenario.id)
    return scenarios


def _assertion_result(assertion: EvalAssertion, output: str) -> AssertionResult:
    flags = 0 if assertion.case_sensitive else re.IGNORECASE
    if assertion.regex:
        matched = re.search(assertion.pattern, output, flags=flags) is not None
    else:
        haystack = output if assertion.case_sensitive else output.casefold()
        needle = (
            assertion.pattern
            if assertion.case_sensitive
            else assertion.pattern.casefold()
        )
        matched = needle in haystack
    passed = matched if assertion.mode == "must" else not matched
    return AssertionResult(
        id=assertion.id,
        mode=assertion.mode,
        pattern=assertion.pattern,
        passed=passed,
    )


def _score(assertions: tuple[AssertionResult, ...]) -> float:
    passed = sum(1 for assertion in assertions if assertion.passed)
    return round((passed / len(assertions)) * 100.0, 2)


def _validate_execution(
    execution: EvalExecution,
    scenario_id: str,
    phase: str,
) -> None:
    if not isinstance(execution.provider, str) or not execution.provider.strip():
        raise EvalError(
            f"Scenario '{scenario_id}' {phase} execution returned no provider identity"
        )
    if not isinstance(execution.model, str) or not execution.model.strip():
        raise EvalError(
            f"Scenario '{scenario_id}' {phase} execution returned no model identity"
        )
    if not isinstance(execution.output, str):
        raise EvalError(
            f"Scenario '{scenario_id}' {phase} execution output must be a string"
        )


def run_behavioral_eval(
    project_root: Path,
    target_type: str,
    target_name: str,
    *,
    executor: EvalExecutor | None = None,
    require_improvement: bool = False,
) -> EvalReport:
    root = project_root.resolve()
    target_content = _target_content(root, target_type, target_name)
    target_digest = sha256(target_content.encode("utf-8")).hexdigest()
    scenarios = load_eval_scenarios(root, target_type, target_name)
    effective_executor: EvalExecutor = executor or MockEvalExecutor()

    scenario_results: list[ScenarioResult] = []
    providers: set[str] = set()
    models: set[str] = set()
    required_failures: list[str] = []
    regressions: list[str] = []

    for scenario in scenarios:
        baseline = effective_executor.execute(
            EvalExecutionRequest(
                target_type=target_type,
                target_name=target_name,
                phase="baseline",
                prompt=scenario.prompt,
                target_content="",
                scenario=scenario,
            )
        )
        candidate = effective_executor.execute(
            EvalExecutionRequest(
                target_type=target_type,
                target_name=target_name,
                phase="candidate",
                prompt=scenario.prompt,
                target_content=target_content,
                scenario=scenario,
            )
        )
        _validate_execution(baseline, scenario.id, "baseline")
        _validate_execution(candidate, scenario.id, "candidate")
        providers.update((baseline.provider, candidate.provider))
        models.update((baseline.model, candidate.model))
        if baseline.provider != candidate.provider or baseline.model != candidate.model:
            raise EvalError(
                f"Scenario '{scenario.id}' baseline and candidate must use the same "
                "provider/model for a comparable eval"
            )
        baseline_assertions = tuple(
            _assertion_result(assertion, baseline.output)
            for assertion in scenario.assertions
        )
        candidate_assertions = tuple(
            _assertion_result(assertion, candidate.output)
            for assertion in scenario.assertions
        )
        baseline_score = _score(baseline_assertions)
        candidate_score = _score(candidate_assertions)
        delta = round(candidate_score - baseline_score, 2)
        candidate_passed = all(
            assertion.passed for assertion in candidate_assertions
        )
        regression = candidate_score < baseline_score
        if scenario.required and not candidate_passed:
            required_failures.append(scenario.id)
        if scenario.required and regression:
            regressions.append(scenario.id)
        scenario_results.append(
            ScenarioResult(
                id=scenario.id,
                description=scenario.description,
                required=scenario.required,
                scenario_sha256=scenario.sha256,
                baseline_output_sha256=sha256(
                    baseline.output.encode("utf-8")
                ).hexdigest(),
                candidate_output_sha256=sha256(
                    candidate.output.encode("utf-8")
                ).hexdigest(),
                baseline_assertions=baseline_assertions,
                candidate_assertions=candidate_assertions,
                baseline_score=baseline_score,
                candidate_score=candidate_score,
                delta=delta,
                passed=candidate_passed,
                regression=regression,
            )
        )

    if len(providers) != 1 or len(models) != 1:
        raise EvalError(
            "All scenarios in one eval report must use one provider/model pair; "
            "run separate reports to compare provider/model variance"
        )
    baseline_score = round(
        sum(result.baseline_score for result in scenario_results)
        / len(scenario_results),
        2,
    )
    candidate_score = round(
        sum(result.candidate_score for result in scenario_results)
        / len(scenario_results),
        2,
    )
    delta = round(candidate_score - baseline_score, 2)
    improvement_satisfied = not require_improvement or delta > 0
    passed = not required_failures and not regressions and improvement_satisfied
    return EvalReport(
        target_type=target_type,
        target_name=target_name,
        target_sha256=target_digest,
        provider=next(iter(providers)),
        model=next(iter(models)),
        scenarios=tuple(scenario_results),
        baseline_score=baseline_score,
        candidate_score=candidate_score,
        delta=delta,
        required_failures=tuple(required_failures),
        regressions=tuple(regressions),
        require_improvement=require_improvement,
        improvement_satisfied=improvement_satisfied,
        passed=passed,
    )
