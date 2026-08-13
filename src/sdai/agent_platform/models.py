from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Capability(StrEnum):
    REQUIREMENTS = "requirements"
    ARCHITECTURE = "architecture"
    PLANNING = "planning"
    CODING = "coding"
    REVIEW = "review"
    TESTING = "testing"
    SECURITY = "security"
    DOCUMENTATION = "documentation"


class ExecutionMode(StrEnum):
    ADVISORY = "advisory"
    WORKSPACE_WRITE = "workspace-write"


@dataclass(frozen=True)
class AgentProfile:
    name: str
    provider: str
    capabilities: tuple[Capability, ...]
    prompt: str
    skills: tuple[str, ...] = ()
    enabled: bool = True
    model: str | None = None
    timeout_seconds: int = 600
    extra_args: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    workspace_write_args: tuple[str, ...] = ()
    environment_allowlist: tuple[str, ...] = ()
    cost_class: str = "standard"
    routing_tier: str = "advanced"
    risk_levels: tuple[str, ...] = ("trivial", "standard", "critical", "regulated")
    complexity_levels: tuple[str, ...] = ("low", "medium", "high", "extreme")
    technologies: tuple[str, ...] = ("*",)
    max_context_chars: int = 1_000_000
    routing_priority: int = 100

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    capabilities: tuple[Capability, ...]
    instructions: str
    root: Path


@dataclass(frozen=True)
class AgentInvocation:
    feature_id: str
    capability: Capability
    profile: AgentProfile
    system: str
    prompt: str
    cwd: Path
    mode: ExecutionMode = ExecutionMode.ADVISORY
    agent_name: str | None = None
    routing_decision: str | None = None


@dataclass(frozen=True)
class AgentExecutionResult:
    feature_id: str
    capability: Capability
    profile: str
    provider: str
    output: str
    prompt: str
    skills: tuple[str, ...] = field(default_factory=tuple)
    agent_name: str | None = None
