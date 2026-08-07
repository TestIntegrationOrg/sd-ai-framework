from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from sdai.models import FeatureContext
from sdai.workflows import is_approved, load_workflow_state


class ConditionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConditionResult:
    matched: bool
    expression: str
    detail: str


def evaluate_condition(
    expression: str | None,
    *,
    context: FeatureContext,
    workflow: str,
    environ: dict[str, str] | None = None,
) -> ConditionResult:
    """Evaluate the deliberately small SD-AI condition DSL.

    Supported forms:
      always
      never
      env:NAME
      env:NAME=value
      artifact:relative/path
      approved:gate
      step:step-id=completed
      not:<expression>

    The DSL intentionally avoids Python/eval so workflow files cannot execute code
    merely by evaluating a condition.
    """
    raw = (expression or "always").strip()
    if not raw:
        raw = "always"

    if raw.startswith("not:"):
        nested = evaluate_condition(
            raw[4:], context=context, workflow=workflow, environ=environ
        )
        return ConditionResult(not nested.matched, raw, f"negated: {nested.detail}")

    if raw == "always":
        return ConditionResult(True, raw, "always")
    if raw == "never":
        return ConditionResult(False, raw, "never")

    if raw.startswith("env:"):
        env = environ if environ is not None else os.environ
        payload = raw[4:]
        if not payload:
            raise ConditionError("env condition requires a variable name")
        if "=" in payload:
            name, expected = payload.split("=", 1)
            name = name.strip()
            if not name:
                raise ConditionError("env condition requires a variable name")
            actual = env.get(name)
            return ConditionResult(
                actual == expected,
                raw,
                f"environment {name}={actual!r}, expected {expected!r}",
            )
        value = env.get(payload)
        matched = value is not None and value.lower() not in {"", "0", "false", "no", "off"}
        return ConditionResult(matched, raw, f"environment {payload}={value!r}")

    if raw.startswith("artifact:"):
        relative = raw[len("artifact:") :].strip()
        candidate = Path(relative)
        if not relative or candidate.is_absolute() or ".." in candidate.parts:
            raise ConditionError("artifact condition must use a relative path inside the feature workspace")
        path = context.artifact(candidate.as_posix())
        return ConditionResult(path.exists(), raw, f"artifact {candidate.as_posix()} exists={path.exists()}")

    if raw.startswith("approved:"):
        gate = raw[len("approved:") :].strip()
        if not gate:
            raise ConditionError("approved condition requires a gate")
        matched = is_approved(context, gate)
        return ConditionResult(matched, raw, f"approval {gate} satisfied={matched}")

    if raw.startswith("step:"):
        payload = raw[len("step:") :].strip()
        if "=" not in payload:
            raise ConditionError("step condition must use step:<id>=completed")
        step_id, expected = (value.strip() for value in payload.split("=", 1))
        if expected != "completed":
            raise ConditionError("step conditions currently support only '=completed'")
        state = load_workflow_state(context, workflow)
        matched = state.is_complete(step_id)
        return ConditionResult(matched, raw, f"step {step_id} completed={matched}")

    raise ConditionError(f"Unsupported workflow condition: {raw}")
