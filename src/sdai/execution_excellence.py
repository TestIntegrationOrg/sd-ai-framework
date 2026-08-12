from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile

from sdai.agent_platform.models import Capability
from sdai.config import load_yaml
from sdai.evals import load_eval_scenarios
from sdai.extensions.manifests import ExtensionKind, load_extension_manifest
from sdai.path_safety import ensure_within_project
from sdai.skill_resolution import load_skill_metadata
from sdai.workflows import load_workflow


class ExecutionExcellenceError(RuntimeError):
    pass


EXECUTION_EXCELLENCE_PACK_ID = "sdai-execution-excellence"
EXECUTION_EXCELLENCE_SKILLS: tuple[str, ...] = (
    "implementation-planning",
    "test-driven-development",
    "systematic-debugging",
    "verification-before-completion",
)


@dataclass(frozen=True)
class ExecutionExcellencePack:
    id: str
    version: str
    skills: tuple[str, ...]
    workflow_examples: tuple[str, ...]
    policy_examples: tuple[str, ...]
    source: str


_SPEC_KEYS = frozenset({"type", "skills", "workflow_examples", "policy_examples"})
_ALLOWED_SEMANTIC_AGENTS = frozenset(
    {"planner", "developer", "code-reviewer", "tester", "architect", "security-reviewer"}
)
_SUPPORTED_CAPABILITIES = frozenset(item.value for item in Capability)
_EXPECTED_REQUIRED: dict[str, frozenset[str]] = {
    "planning": frozenset({"implementation-planning"}),
    "coding": frozenset({"test-driven-development", "verification-before-completion"}),
    "review": frozenset({"verification-before-completion"}),
    "testing": frozenset({"verification-before-completion"}),
}


def _fail(code: str, message: str) -> ExecutionExcellenceError:
    return ExecutionExcellenceError(f"{code}: {message}")


def _portable(root: Path, path: Path) -> str:
    safe = ensure_within_project(root, path, label="execution-excellence path")
    return safe.relative_to(root.resolve()).as_posix()


def _strings(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise _fail("SDAI-EXEC-001", f"{label} must be a non-empty string list")
    values = tuple(item.strip() for item in value)
    if len(values) != len(set(values)):
        raise _fail("SDAI-EXEC-001", f"{label} must not contain duplicates")
    return values


def _example_path(root: Path, value: str, *, label: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise _fail("SDAI-EXEC-001", f"{label} must be a repository-relative path")
    path = ensure_within_project(root, root / candidate, label=label)
    if not path.is_file():
        raise _fail("SDAI-EXEC-004", f"{label} does not exist: {value}")
    return path


def _validate_with_workflow_engine(path: Path, source: str) -> None:
    """Prove the example is consumable by the current public v5 workflow parser."""

    with tempfile.TemporaryDirectory(prefix="sdai-execution-workflow-") as directory:
        temp_root = Path(directory)
        target = temp_root / ".sdai" / "workflows" / f"{path.stem}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        try:
            load_workflow(temp_root, path.stem)
        except RuntimeError as exc:
            raise _fail(
                "SDAI-EXEC-004",
                f"workflow example '{source}' is not valid for the current workflow engine: {exc}",
            ) from exc


def _validate_workflow_example(root: Path, source: str) -> None:
    path = _example_path(root, source, label="execution workflow example")
    raw = load_yaml(path)
    if raw.get("version") != 5 or raw.get("validation_mode") not in {
        "light",
        "standard",
        "critical",
    }:
        raise _fail(
            "SDAI-EXEC-004",
            f"workflow example '{source}' must use workflow version 5 and a valid validation_mode",
        )
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise _fail("SDAI-EXEC-004", f"workflow example '{source}' must define steps")
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise _fail(
                "SDAI-EXEC-004",
                f"workflow example '{source}' step #{index} must be a mapping",
            )
        if "profile" in step:
            raise _fail(
                "SDAI-EXEC-005",
                f"workflow example '{source}' must not pin an AI provider profile",
            )
        if "skills" in step:
            raise _fail(
                "SDAI-EXEC-004",
                f"workflow example '{source}' must not use unsupported per-step skills syntax",
            )
        if step.get("type") == "agent":
            agent = str(step.get("agent") or "")
            if agent not in _ALLOWED_SEMANTIC_AGENTS:
                raise _fail(
                    "SDAI-EXEC-005",
                    f"workflow example '{source}' uses non-semantic agent '{agent}'",
                )
    if steps[-1].get("type") != "validate":
        raise _fail(
            "SDAI-EXEC-004",
            f"workflow example '{source}' must end with deterministic validation",
        )
    _validate_with_workflow_engine(path, source)


def _validate_policy_example(root: Path, source: str) -> None:
    path = _example_path(root, source, label="execution policy example")
    raw = load_yaml(path)
    if raw.get("version") != 1:
        raise _fail("SDAI-EXEC-004", f"policy example '{source}' version must be 1")
    providers = raw.get("providers", {})
    if providers not in ({}, None):
        raise _fail(
            "SDAI-EXEC-005",
            f"policy example '{source}' must not select or constrain providers",
        )
    skills = raw.get("skills") or {}
    required = skills.get("required") if isinstance(skills, dict) else None
    if not isinstance(required, dict) or not required:
        raise _fail(
            "SDAI-EXEC-004",
            f"policy example '{source}' must declare required execution skills",
        )

    normalized: dict[str, tuple[str, ...]] = {}
    for capability, values in required.items():
        if not isinstance(capability, str) or not capability.strip():
            raise _fail(
                "SDAI-EXEC-004",
                f"policy example '{source}' contains an invalid capability key",
            )
        capability_name = capability.strip()
        if capability_name not in _SUPPORTED_CAPABILITIES:
            raise _fail(
                "SDAI-EXEC-004",
                f"policy example '{source}' uses unsupported capability '{capability_name}'",
            )
        normalized[capability_name] = _strings(
            values,
            label=f"policy example '{source}' skills.required.{capability_name}",
        )

    referenced = {name for values in normalized.values() for name in values}
    unexpected = sorted(referenced - set(EXECUTION_EXCELLENCE_SKILLS))
    if unexpected:
        raise _fail(
            "SDAI-EXEC-004",
            f"policy example '{source}' references skills outside the pack: {', '.join(unexpected)}",
        )
    for capability, expected in _EXPECTED_REQUIRED.items():
        actual = set(normalized.get(capability, ()))
        missing = sorted(expected - actual)
        if missing:
            raise _fail(
                "SDAI-EXEC-004",
                f"policy example '{source}' must require for {capability}: {', '.join(missing)}",
            )


def load_execution_excellence_pack(project_root: Path) -> ExecutionExcellencePack:
    root = project_root.resolve()
    path = ensure_within_project(
        root,
        root / ".sdai" / "extensions" / "packs" / f"{EXECUTION_EXCELLENCE_PACK_ID}.yaml",
        label="execution-excellence manifest",
    )
    try:
        manifest = load_extension_manifest(root, path)
    except RuntimeError as exc:
        raise _fail(
            "SDAI-EXEC-001", f"invalid execution-excellence manifest: {exc}"
        ) from exc
    if (
        manifest.kind is not ExtensionKind.PACK
        or manifest.metadata.id != EXECUTION_EXCELLENCE_PACK_ID
    ):
        raise _fail(
            "SDAI-EXEC-001",
            "execution-excellence asset must use the expected Pack identity",
        )
    unknown = sorted(set(manifest.spec) - _SPEC_KEYS)
    if unknown:
        raise _fail(
            "SDAI-EXEC-001",
            "execution-excellence pack contains unsupported spec key(s): "
            + ", ".join(unknown),
        )
    if manifest.spec.get("type") != "execution":
        raise _fail(
            "SDAI-EXEC-001", "execution-excellence pack spec.type must be 'execution'"
        )

    skills = _strings(
        manifest.spec.get("skills"), label="execution-excellence skills"
    )
    if skills != EXECUTION_EXCELLENCE_SKILLS:
        raise _fail(
            "SDAI-EXEC-002",
            "execution-excellence skills must be exactly: "
            + ", ".join(EXECUTION_EXCELLENCE_SKILLS),
        )
    for name in skills:
        try:
            metadata = load_skill_metadata(root, name)
            load_eval_scenarios(root, "skill", name)
        except RuntimeError as exc:
            raise _fail(
                "SDAI-EXEC-002", f"execution skill '{name}' is invalid: {exc}"
            ) from exc
        if metadata.compatibility:
            raise _fail(
                "SDAI-EXEC-003",
                f"execution skill '{name}' must remain technology-neutral",
            )
        if metadata.compatible_agents:
            raise _fail(
                "SDAI-EXEC-003",
                f"execution skill '{name}' must remain semantic-role neutral",
            )
        if not metadata.capabilities:
            raise _fail(
                "SDAI-EXEC-003",
                f"execution skill '{name}' must declare lifecycle capabilities",
            )
        if not metadata.selection.auto:
            raise _fail(
                "SDAI-EXEC-003",
                f"execution skill '{name}' must opt into resolver auto-selection",
            )

    workflows = _strings(
        manifest.spec.get("workflow_examples"), label="execution workflow examples"
    )
    policies = _strings(
        manifest.spec.get("policy_examples"), label="execution policy examples"
    )
    for source in workflows:
        _validate_workflow_example(root, source)
    for source in policies:
        _validate_policy_example(root, source)

    return ExecutionExcellencePack(
        id=manifest.metadata.id,
        version=manifest.metadata.version,
        skills=skills,
        workflow_examples=workflows,
        policy_examples=policies,
        source=_portable(root, path),
    )
