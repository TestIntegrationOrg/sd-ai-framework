from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any

import yaml

from sdai.artifacts import write_text
from sdai.config import load_yaml
from sdai.models import FeatureContext


class QualityGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class QualityGateDefinition:
    name: str
    command: tuple[str, ...]
    enabled: bool = True
    timeout_seconds: int = 900
    success_exit_codes: tuple[int, ...] = (0,)


@dataclass(frozen=True)
class QualityGateResult:
    name: str
    command: tuple[str, ...]
    return_code: int
    passed: bool
    output: str
    artifact: Path | None = None


def load_quality_gates(project_root: Path) -> dict[str, QualityGateDefinition]:
    path = project_root / ".sdai" / "quality-gates.yaml"
    if not path.exists():
        return {}
    data = load_yaml(path)
    raw_gates = data.get("gates") or {}
    if not isinstance(raw_gates, dict):
        raise QualityGateError("quality-gates.yaml 'gates' must be a mapping")

    result: dict[str, QualityGateDefinition] = {}
    for name, raw in raw_gates.items():
        if not isinstance(raw, dict):
            raise QualityGateError(f"Quality gate '{name}' must be a mapping")
        command = raw.get("command") or []
        if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
            raise QualityGateError(f"Quality gate '{name}' must define command as a non-empty string list")
        success = raw.get("success_exit_codes", [0])
        if not isinstance(success, list) or not success:
            raise QualityGateError(f"Quality gate '{name}' success_exit_codes must be a non-empty list")
        result[str(name)] = QualityGateDefinition(
            name=str(name),
            command=tuple(command),
            enabled=bool(raw.get("enabled", True)),
            timeout_seconds=int(raw.get("timeout_seconds", 900)),
            success_exit_codes=tuple(int(v) for v in success),
        )
    return result


class QualityGateRunner:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()

    def definition(self, name: str) -> QualityGateDefinition:
        gates = load_quality_gates(self.project_root)
        if name not in gates:
            raise QualityGateError(f"Unknown quality gate: {name}")
        return gates[name]

    def run(self, name: str, *, context: FeatureContext | None = None) -> QualityGateResult:
        gate = self.definition(name)
        if not gate.enabled:
            raise QualityGateError(f"Quality gate '{name}' is disabled")
        try:
            completed = subprocess.run(
                list(gate.command),
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=gate.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise QualityGateError(f"Quality gate executable not found: {gate.command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise QualityGateError(
                f"Quality gate '{name}' timed out after {gate.timeout_seconds} seconds"
            ) from exc

        output = (completed.stdout or "")
        if completed.stderr:
            if output and not output.endswith("\n"):
                output += "\n"
            output += completed.stderr
        passed = completed.returncode in gate.success_exit_codes
        artifact: Path | None = None
        if context is not None:
            artifact = context.artifact(f"quality-gates/{name}.md")
            write_text(
                artifact,
                f"# Quality Gate — {name}\n\n"
                f"- Passed: {str(passed).lower()}\n"
                f"- Exit code: {completed.returncode}\n"
                f"- Command: `{_display_command(gate.command)}`\n\n"
                f"## Output\n\n```text\n{output.rstrip()}\n```\n",
            )
        return QualityGateResult(
            name=name,
            command=gate.command,
            return_code=completed.returncode,
            passed=passed,
            output=output,
            artifact=artifact,
        )


def _display_command(command: tuple[str, ...]) -> str:
    # This is display-only; execution never goes through a shell.
    return " ".join(value.replace("`", "\\`") for value in command)


def scaffold_quality_gates() -> str:
    data: dict[str, Any] = {
        "version": 1,
        "gates": {
            "tests": {
                "enabled": True,
                "command": ["pytest", "-q"],
                "timeout_seconds": 900,
                "success_exit_codes": [0],
            },
            "trivy": {
                "enabled": False,
                "command": ["trivy", "fs", "--exit-code", "1", "--severity", "HIGH,CRITICAL", "."],
                "timeout_seconds": 1200,
                "success_exit_codes": [0],
            },
            "sonar": {
                "enabled": False,
                "command": ["sonar-scanner", "-Dsonar.qualitygate.wait=true"],
                "timeout_seconds": 1800,
                "success_exit_codes": [0],
            },
        },
    }
    return yaml.safe_dump(data, sort_keys=False)
