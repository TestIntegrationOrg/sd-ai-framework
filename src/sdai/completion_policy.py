from __future__ import annotations

from enum import Enum
import os
from pathlib import Path
from typing import Mapping

import yaml


COMPLETION_POLICY_API_VERSION = "sdai.completion-policy/v1"


class CompletionPolicyError(RuntimeError):
    pass


class CompletionStage(str, Enum):
    TASK = "task"
    CHANGE = "change"


class CompletionDimension(str, Enum):
    SPEC_REVIEW = "spec-review"
    CODE_QUALITY_REVIEW = "code-quality-review"
    FINAL_REVIEW = "final-review"
    VERIFICATION = "verification"
    TEST = "test"
    QUALITY = "quality"
    SECURITY = "security"
    APPROVAL = "approval"


_RISKS = frozenset({"trivial", "standard", "critical", "regulated"})
_BUILTIN_TASK = {
    "trivial": frozenset({CompletionDimension.SPEC_REVIEW, CompletionDimension.CODE_QUALITY_REVIEW}),
    "standard": frozenset({CompletionDimension.SPEC_REVIEW, CompletionDimension.CODE_QUALITY_REVIEW, CompletionDimension.TEST, CompletionDimension.QUALITY}),
    "critical": frozenset({CompletionDimension.SPEC_REVIEW, CompletionDimension.CODE_QUALITY_REVIEW, CompletionDimension.TEST, CompletionDimension.QUALITY, CompletionDimension.SECURITY}),
    "regulated": frozenset({CompletionDimension.SPEC_REVIEW, CompletionDimension.CODE_QUALITY_REVIEW, CompletionDimension.TEST, CompletionDimension.QUALITY, CompletionDimension.SECURITY, CompletionDimension.APPROVAL}),
}
_BUILTIN_CHANGE = {
    "trivial": frozenset({CompletionDimension.FINAL_REVIEW, CompletionDimension.VERIFICATION}),
    "standard": frozenset({CompletionDimension.FINAL_REVIEW, CompletionDimension.VERIFICATION}),
    "critical": frozenset({CompletionDimension.FINAL_REVIEW, CompletionDimension.VERIFICATION, CompletionDimension.SECURITY}),
    "regulated": frozenset({CompletionDimension.FINAL_REVIEW, CompletionDimension.VERIFICATION, CompletionDimension.SECURITY, CompletionDimension.APPROVAL}),
}


def _fail(message: str) -> CompletionPolicyError:
    return CompletionPolicyError(f"SDAI-COMPLETE-POLICY-001: {message}")


def normalize_risk(value: str) -> str:
    risk = value.strip().lower() if isinstance(value, str) else ""
    if risk not in _RISKS:
        raise _fail(f"risk must be one of: {', '.join(sorted(_RISKS))}")
    return risk


def _layer(path: Path, risk: str, stage: CompletionStage) -> frozenset[CompletionDimension]:
    if not path.exists():
        return frozenset()
    if path.is_symlink() or not path.is_file():
        raise _fail(f"policy must be a regular non-symlink file: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise _fail(f"invalid policy {path}: {exc}") from exc
    if raw is None:
        return frozenset()
    if not isinstance(raw, Mapping) or set(raw) != {"apiVersion", "risks"}:
        raise _fail(f"policy fields are invalid: {path}")
    if raw.get("apiVersion") != COMPLETION_POLICY_API_VERSION or not isinstance(raw.get("risks"), Mapping):
        raise _fail(f"unsupported policy contract: {path}")
    risks = raw["risks"]
    unknown = set(risks) - _RISKS
    if unknown:
        raise _fail(f"unsupported risk keys: {sorted(unknown)}")
    selected = risks.get(risk, {})
    if not isinstance(selected, Mapping) or set(selected) - {"task", "change"}:
        raise _fail(f"policy risk {risk!r} has invalid stage fields")
    values = selected.get(stage.value, [])
    if not isinstance(values, list):
        raise _fail(f"policy {risk}.{stage.value} must be a list")
    result: set[CompletionDimension] = set()
    for value in values:
        try:
            result.add(CompletionDimension(value))
        except (ValueError, TypeError) as exc:
            raise _fail(f"unsupported completion dimension: {value!r}") from exc
    return frozenset(result)


def required_dimensions(
    project_root: Path,
    risk: str,
    stage: CompletionStage,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[CompletionDimension, ...]:
    """Resolve built-in -> org -> repo -> user requirements by monotonic set union."""
    root = project_root.resolve()
    selected_risk = normalize_risk(risk)
    selected_stage = stage if isinstance(stage, CompletionStage) else CompletionStage(stage)
    env = dict(os.environ if environ is None else environ)
    builtins = _BUILTIN_TASK if selected_stage is CompletionStage.TASK else _BUILTIN_CHANGE
    required = set(builtins[selected_risk])
    paths: list[Path] = []
    if env.get("SDAI_ORG_COMPLETION_POLICY_PATH"):
        paths.append(Path(env["SDAI_ORG_COMPLETION_POLICY_PATH"]).expanduser().resolve())
    paths.append(root / ".sdai" / "completion-policy.yaml")
    if env.get("SDAI_USER_COMPLETION_POLICY_PATH"):
        paths.append(Path(env["SDAI_USER_COMPLETION_POLICY_PATH"]).expanduser().resolve())
    for path in paths:
        required.update(_layer(path, selected_risk, selected_stage))
    return tuple(sorted(required, key=lambda item: item.value))


__all__ = [
    "COMPLETION_POLICY_API_VERSION",
    "CompletionDimension",
    "CompletionPolicyError",
    "CompletionStage",
    "normalize_risk",
    "required_dimensions",
]
