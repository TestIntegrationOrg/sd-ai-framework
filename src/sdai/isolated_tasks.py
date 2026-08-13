from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from typing import Mapping, Sequence

from sdai.agent_platform import AgentRuntime
from sdai.agent_platform.models import (
    AgentExecutionResult,
    AgentInvocation,
    Capability,
    ExecutionMode,
)
from sdai.convergence import RemediationTask
from sdai.execution_ledger import ExecutionLedger, HashBinding
from sdai.execution_resume import resume_execution
from sdai.models import validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.text import TextEncodingError, read_utf8_text


ISOLATED_TASK_API_VERSION = "sdai.isolated-task/v1"
ISOLATED_INVOCATION_API_VERSION = "sdai.isolated-invocation/v1"
ISOLATED_RESULT_API_VERSION = "sdai.isolated-result/v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_INVOCATION_ID = re.compile(r"^INVOCATION-[0-9a-f]{24}$")
_GIT_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_CONTEXT_ITEMS = 64
_MAX_CONTEXT_CHARS = 60_000
_CONTEXT_WINDOW = 4


class IsolatedTaskError(RuntimeError):
    """Raised when isolated task-agent state is unsafe, stale, or ambiguous."""


class IsolatedStage(str, Enum):
    IMPLEMENT = "implement"
    SPEC_COMPLIANCE_REVIEW = "spec-compliance-review"
    CODE_QUALITY_REVIEW = "code-quality-review"
    FINAL_CHANGE_REVIEW = "final-change-review"


class IsolatedStageStatus(str, Enum):
    RECORDED = "recorded"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


_STAGE_AGENT: Mapping[IsolatedStage, tuple[str, Capability, ExecutionMode]] = {
    IsolatedStage.IMPLEMENT: ("developer", Capability.CODING, ExecutionMode.WORKSPACE_WRITE),
    IsolatedStage.SPEC_COMPLIANCE_REVIEW: (
        "code-reviewer",
        Capability.REVIEW,
        ExecutionMode.ADVISORY,
    ),
    IsolatedStage.CODE_QUALITY_REVIEW: (
        "code-reviewer",
        Capability.REVIEW,
        ExecutionMode.ADVISORY,
    ),
    IsolatedStage.FINAL_CHANGE_REVIEW: (
        "code-reviewer",
        Capability.REVIEW,
        ExecutionMode.ADVISORY,
    ),
}

_STAGE_INSTRUCTIONS: Mapping[IsolatedStage, str] = {
    IsolatedStage.IMPLEMENT: (
        "Implement only this durable remediation task. Do not broaden scope. "
        "Do not modify any forbidden root. Treat the bound context below as the complete "
        "task context; do not assume prior chat or hidden conversation history."
    ),
    IsolatedStage.SPEC_COMPLIANCE_REVIEW: (
        "Independently review whether the implementation satisfies the exact remediation "
        "task and cited specification evidence. Focus only on spec compliance; do not waive "
        "a requirement because the implementation chose different behavior."
    ),
    IsolatedStage.CODE_QUALITY_REVIEW: (
        "Independently review the accepted implementation for correctness, regression risk, "
        "security, tests, maintainability, and architecture quality. Do not repeat or replace "
        "the spec-compliance decision."
    ),
    IsolatedStage.FINAL_CHANGE_REVIEW: (
        "Perform an independent whole-change review only after every remediation task has "
        "passed spec-compliance and code-quality review. Check cross-task interactions, "
        "regressions, traceability, and release-level risk."
    ),
}


def _fail(code: str, message: str) -> IsolatedTaskError:
    return IsolatedTaskError(f"{code}: {message}")


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-ISOLATED-001", f"isolated task record is not canonical JSON: {exc}") from exc


def _hash(value: Mapping[str, object]) -> str:
    return "sha256:" + sha256(_canonical_bytes(value)).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise _fail("SDAI-ISOLATED-002", f"{label} must be canonical sha256:<64 lowercase hex>")
    return value


def _text(value: object, *, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-ISOLATED-002", f"{label} must be non-empty text")
    result = value.strip()
    if "\x00" in result or len(result) > maximum:
        raise _fail("SDAI-ISOLATED-002", f"{label} is invalid or too long")
    return result


def _commit(value: object) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-ISOLATED-002", "git_commit must be a string")
    result = value.strip().casefold()
    if not _GIT_COMMIT.fullmatch(result):
        raise _fail("SDAI-ISOLATED-002", f"invalid Git commit: {value!r}")
    return result


def _portable_path(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-ISOLATED-002", f"{label} must be a repository-relative path")
    source = value.strip()
    if "\\" in source or source.startswith("/") or re.match(r"^[A-Za-z]:", source):
        raise _fail("SDAI-ISOLATED-002", f"{label} must be a repository-relative POSIX path")
    path = PurePosixPath(source)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise _fail("SDAI-ISOLATED-002", f"{label} contains an unsafe path segment")
    return path.as_posix()


def _git_executable() -> str:
    candidate = shutil.which("git")
    if not candidate:
        raise _fail("SDAI-ISOLATED-003", "Git executable is unavailable")
    resolved = Path(candidate).resolve()
    if not resolved.is_file():
        raise _fail("SDAI-ISOLATED-003", "resolved Git executable is not a file")
    return str(resolved)


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    dangerous = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
    }
    for key in list(env):
        upper = key.upper()
        if (
            upper in dangerous
            or upper.startswith("GIT_CONFIG_KEY_")
            or upper.startswith("GIT_CONFIG_VALUE_")
            or upper == "GIT_CONFIG_COUNT"
        ):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        [_git_executable(), *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        shell=False,
        check=False,
        env=_git_env(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        raise _fail("SDAI-ISOLATED-003", f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _head(root: Path) -> str:
    return _commit(_git(root, "rev-parse", "--verify", "HEAD"))


@dataclass(frozen=True)
class IsolatedContextSlice:
    source: str
    line_start: int
    line_end: int
    source_sha256: str
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _portable_path(self.source, label="context source"))
        if (
            not isinstance(self.line_start, int)
            or isinstance(self.line_start, bool)
            or self.line_start < 1
            or not isinstance(self.line_end, int)
            or isinstance(self.line_end, bool)
            or self.line_end < self.line_start
        ):
            raise _fail("SDAI-ISOLATED-004", "context line range is invalid")
        object.__setattr__(self, "source_sha256", _sha(self.source_sha256, label="context source_sha256"))
        if not isinstance(self.text, str) or "\x00" in self.text or len(self.text) > _MAX_CONTEXT_CHARS:
            raise _fail("SDAI-ISOLATED-004", "context text is invalid or too large")

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "source_sha256": self.source_sha256,
            "text": self.text,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "IsolatedContextSlice":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-ISOLATED-004", "context slice must be a mapping")
        expected = {"source", "line_start", "line_end", "source_sha256", "text"}
        if set(value) != expected:
            raise _fail("SDAI-ISOLATED-004", "context slice fields do not match contract")
        return cls(
            source=value["source"],
            line_start=value["line_start"],
            line_end=value["line_end"],
            source_sha256=value["source_sha256"],
            text=value["text"],
        )


@dataclass(frozen=True)
class IsolatedTaskContract:
    feature_id: str
    task_id: str
    remediation_task_sha256: str
    round_id: str
    attempt: int
    stage: IsolatedStage
    git_commit: str
    dispatch_id: str
    semantic_agent: str
    capability: Capability
    mode: ExecutionMode
    summary: str
    allowed_roots: tuple[str, ...]
    forbidden_roots: tuple[str, ...]
    context: tuple[IsolatedContextSlice, ...]
    predecessor_invocation_ids: tuple[str, ...] = ()
    worker_invocation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", validate_feature_id(self.feature_id))
        if not isinstance(self.task_id, str) or not _TASK_ID.fullmatch(self.task_id):
            raise _fail("SDAI-ISOLATED-005", f"invalid isolated task id: {self.task_id!r}")
        object.__setattr__(
            self,
            "remediation_task_sha256",
            _sha(self.remediation_task_sha256, label="remediation_task_sha256"),
        )
        object.__setattr__(self, "round_id", _text(self.round_id, label="round_id", maximum=128))
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt < 1:
            raise _fail("SDAI-ISOLATED-005", "isolated task attempt must be positive")
        try:
            stage = self.stage if isinstance(self.stage, IsolatedStage) else IsolatedStage(self.stage)
            capability = self.capability if isinstance(self.capability, Capability) else Capability(self.capability)
            mode = self.mode if isinstance(self.mode, ExecutionMode) else ExecutionMode(self.mode)
        except ValueError as exc:
            raise _fail("SDAI-ISOLATED-005", "invalid isolated task stage/capability/mode") from exc
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "mode", mode)
        expected_agent, expected_capability, expected_mode = _STAGE_AGENT[stage]
        if self.semantic_agent != expected_agent:
            raise _fail(
                "SDAI-ISOLATED-005",
                f"stage {stage.value} requires semantic agent {expected_agent!r}",
            )
        if capability is not expected_capability or mode is not expected_mode:
            raise _fail("SDAI-ISOLATED-005", f"stage {stage.value} has fixed capability/execution mode")
        object.__setattr__(self, "git_commit", _commit(self.git_commit))
        object.__setattr__(self, "dispatch_id", _text(self.dispatch_id, label="dispatch_id", maximum=512))
        object.__setattr__(self, "summary", _text(self.summary, label="task summary"))
        allowed = tuple(sorted({_portable_path(item, label="allowed root") for item in self.allowed_roots}))
        forbidden = tuple(sorted({_portable_path(item, label="forbidden root") for item in self.forbidden_roots}))
        if not allowed or not forbidden or set(allowed) & set(forbidden):
            raise _fail("SDAI-ISOLATED-005", "isolated task roots must be non-empty and non-overlapping")
        requirement_truth = f"specs/changes/{self.feature_id}/requirements.md"
        if requirement_truth not in forbidden or "specs/current" not in forbidden:
            raise _fail("SDAI-ISOLATED-005", "isolated task must preserve requirements/current specification truth")
        object.__setattr__(self, "allowed_roots", allowed)
        object.__setattr__(self, "forbidden_roots", forbidden)
        if len(self.context) > _MAX_CONTEXT_ITEMS:
            raise _fail("SDAI-ISOLATED-005", "isolated task contains too many context slices")
        total = sum(len(item.text) for item in self.context)
        if total > _MAX_CONTEXT_CHARS:
            raise _fail("SDAI-ISOLATED-005", "isolated task context exceeds bounded context limit")
        object.__setattr__(
            self,
            "context",
            tuple(sorted(self.context, key=lambda item: (item.source.casefold(), item.source, item.line_start, item.line_end))),
        )
        predecessor = tuple(sorted(set(self.predecessor_invocation_ids)))
        if any(not _INVOCATION_ID.fullmatch(item) for item in predecessor):
            raise _fail("SDAI-ISOLATED-005", "invalid predecessor invocation id")
        object.__setattr__(self, "predecessor_invocation_ids", predecessor)
        if self.worker_invocation_id is not None and not _INVOCATION_ID.fullmatch(self.worker_invocation_id):
            raise _fail("SDAI-ISOLATED-005", "invalid worker invocation id")
        if stage is IsolatedStage.IMPLEMENT:
            if predecessor or self.worker_invocation_id is not None:
                raise _fail("SDAI-ISOLATED-005", "implementation stage cannot inherit predecessor/worker invocations")
        else:
            if self.worker_invocation_id is None:
                raise _fail("SDAI-ISOLATED-005", "review stages require the independent worker invocation id")
            if self.worker_invocation_id in predecessor:
                # The worker ID is carried separately so reviewers cannot accidentally
                # treat the worker invocation as a prior reviewer approval.
                raise _fail("SDAI-ISOLATED-005", "worker invocation must not be a predecessor review approval")

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": ISOLATED_TASK_API_VERSION,
            "feature_id": self.feature_id,
            "task_id": self.task_id,
            "remediation_task_sha256": self.remediation_task_sha256,
            "round_id": self.round_id,
            "attempt": self.attempt,
            "stage": self.stage.value,
            "git_commit": self.git_commit,
            "dispatch_id": self.dispatch_id,
            "semantic_agent": self.semantic_agent,
            "capability": self.capability.value,
            "mode": self.mode.value,
            "summary": self.summary,
            "allowed_roots": list(self.allowed_roots),
            "forbidden_roots": list(self.forbidden_roots),
            "context": [item.as_dict() for item in self.context],
            "predecessor_invocation_ids": list(self.predecessor_invocation_ids),
            "worker_invocation_id": self.worker_invocation_id,
        }

    @property
    def sha256(self) -> str:
        return _hash(self.body_dict())

    def as_dict(self) -> dict[str, object]:
        result = self.body_dict()
        result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8")

    def prompt_context(self) -> str:
        lines = [
            "# SDAI Isolated Task Contract",
            f"Task: {self.task_id}",
            f"Stage: {self.stage.value}",
            f"Attempt: {self.attempt}",
            f"Contract SHA-256: {self.sha256}",
            f"Dispatch: {self.dispatch_id}",
            "",
            _STAGE_INSTRUCTIONS[self.stage],
            "",
            "Task summary:",
            self.summary,
            "",
            "Allowed write roots:",
            *[f"- {item}" for item in self.allowed_roots],
            "Forbidden roots:",
            *[f"- {item}" for item in self.forbidden_roots],
            "",
            "Bound context (this is the complete task context; no chat history is inherited):",
        ]
        if not self.context:
            lines.append("- No file context is required for this stage.")
        for item in self.context:
            lines.extend(
                [
                    "",
                    f"## {item.source}:{item.line_start}-{item.line_end}",
                    f"source_sha256={item.source_sha256}",
                    item.text,
                ]
            )
        if self.worker_invocation_id:
            lines.extend(["", f"Worker invocation under review: {self.worker_invocation_id}"])
        if self.predecessor_invocation_ids:
            lines.extend(
                ["Prior independent review invocations:", *[f"- {item}" for item in self.predecessor_invocation_ids]]
            )
        return "\n".join(lines).rstrip() + "\n"

    @classmethod
    def from_mapping(cls, value: object) -> "IsolatedTaskContract":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-ISOLATED-005", "isolated task contract must be a mapping")
        expected = {
            "apiVersion", "feature_id", "task_id", "remediation_task_sha256", "round_id",
            "attempt", "stage", "git_commit", "dispatch_id", "semantic_agent", "capability",
            "mode", "summary", "allowed_roots", "forbidden_roots", "context",
            "predecessor_invocation_ids", "worker_invocation_id", "sha256",
        }
        if set(value) != expected or value.get("apiVersion") != ISOLATED_TASK_API_VERSION:
            raise _fail("SDAI-ISOLATED-005", "isolated task contract fields/apiVersion do not match")
        for name in ("allowed_roots", "forbidden_roots", "context", "predecessor_invocation_ids"):
            if not isinstance(value[name], list):
                raise _fail("SDAI-ISOLATED-005", f"isolated task {name} must be a list")
        contract = cls(
            feature_id=value["feature_id"], task_id=value["task_id"],
            remediation_task_sha256=value["remediation_task_sha256"], round_id=value["round_id"],
            attempt=value["attempt"], stage=value["stage"], git_commit=value["git_commit"],
            dispatch_id=value["dispatch_id"], semantic_agent=value["semantic_agent"],
            capability=value["capability"], mode=value["mode"], summary=value["summary"],
            allowed_roots=tuple(value["allowed_roots"]), forbidden_roots=tuple(value["forbidden_roots"]),
            context=tuple(IsolatedContextSlice.from_mapping(item) for item in value["context"]),
            predecessor_invocation_ids=tuple(value["predecessor_invocation_ids"]),
            worker_invocation_id=value["worker_invocation_id"],
        )
        if value["sha256"] != contract.sha256:
            raise _fail("SDAI-ISOLATED-005", "isolated task contract SHA-256 mismatch")
        return contract

    @classmethod
    def from_json(cls, value: str | bytes) -> "IsolatedTaskContract":
        try:
            text = value.decode("utf-8") if isinstance(value, bytes) else value
            return cls.from_mapping(json.loads(text))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise _fail("SDAI-ISOLATED-005", f"invalid isolated task JSON: {exc}") from exc


@dataclass(frozen=True)
class IsolatedInvocationRecord:
    invocation_id: str
    contract_sha256: str
    stage: IsolatedStage
    semantic_agent: str
    capability: Capability
    mode: ExecutionMode
    profile: str
    provider: str
    model: str | None
    system_sha256: str
    prompt_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.invocation_id, str) or not _INVOCATION_ID.fullmatch(self.invocation_id):
            raise _fail("SDAI-ISOLATED-006", f"invalid invocation id: {self.invocation_id!r}")
        object.__setattr__(self, "contract_sha256", _sha(self.contract_sha256, label="contract_sha256"))
        try:
            object.__setattr__(self, "stage", self.stage if isinstance(self.stage, IsolatedStage) else IsolatedStage(self.stage))
            object.__setattr__(self, "capability", self.capability if isinstance(self.capability, Capability) else Capability(self.capability))
            object.__setattr__(self, "mode", self.mode if isinstance(self.mode, ExecutionMode) else ExecutionMode(self.mode))
        except ValueError as exc:
            raise _fail("SDAI-ISOLATED-006", "invalid invocation stage/capability/mode") from exc
        for label in ("semantic_agent", "profile", "provider"):
            object.__setattr__(self, label, _text(getattr(self, label), label=label, maximum=256))
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, label="model", maximum=256))
        object.__setattr__(self, "system_sha256", _sha(self.system_sha256, label="system_sha256"))
        object.__setattr__(self, "prompt_sha256", _sha(self.prompt_sha256, label="prompt_sha256"))

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": ISOLATED_INVOCATION_API_VERSION,
            "invocation_id": self.invocation_id,
            "contract_sha256": self.contract_sha256,
            "stage": self.stage.value,
            "semantic_agent": self.semantic_agent,
            "capability": self.capability.value,
            "mode": self.mode.value,
            "profile": self.profile,
            "provider": self.provider,
            "model": self.model,
            "system_sha256": self.system_sha256,
            "prompt_sha256": self.prompt_sha256,
        }


@dataclass(frozen=True)
class IsolatedStageResult:
    invocation: IsolatedInvocationRecord
    status: IsolatedStageStatus
    git_commit: str
    output: str

    def __post_init__(self) -> None:
        if not isinstance(self.invocation, IsolatedInvocationRecord):
            raise _fail("SDAI-ISOLATED-007", "isolated result requires invocation record")
        try:
            object.__setattr__(
                self,
                "status",
                self.status if isinstance(self.status, IsolatedStageStatus) else IsolatedStageStatus(self.status),
            )
        except ValueError as exc:
            raise _fail("SDAI-ISOLATED-007", "invalid isolated stage status") from exc
        object.__setattr__(self, "git_commit", _commit(self.git_commit))
        if not isinstance(self.output, str) or "\x00" in self.output or len(self.output) > 1_000_000:
            raise _fail("SDAI-ISOLATED-007", "isolated result output is invalid or too large")

    @property
    def output_sha256(self) -> str:
        return _hash_bytes(self.output.encode("utf-8"))

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": ISOLATED_RESULT_API_VERSION,
            "invocation": self.invocation.as_dict(),
            "status": self.status.value,
            "git_commit": self.git_commit,
            "output": self.output,
            "output_sha256": self.output_sha256,
        }

    @property
    def sha256(self) -> str:
        return _hash(self.body_dict())

    def as_dict(self) -> dict[str, object]:
        result = self.body_dict()
        result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8")


@dataclass(frozen=True)
class PreparedIsolatedInvocation:
    contract: IsolatedTaskContract
    invocation: AgentInvocation
    record: IsolatedInvocationRecord


@dataclass(frozen=True)
class IsolatedDispatch:
    dispatch_id: str
    reused: bool
    attempt: int


def _safe_source(root: Path, source: str) -> Path:
    try:
        candidate = ensure_within_project(root, root.joinpath(*PurePosixPath(source).parts), label="isolated context source")
    except PathSafetyError as exc:
        raise _fail("SDAI-ISOLATED-008", f"context source escapes project root: {source}") from exc
    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise _fail("SDAI-ISOLATED-008", f"context source contains symlink component: {source}")
    if candidate.is_symlink() or not candidate.is_file():
        raise _fail("SDAI-ISOLATED-008", f"context source must be a regular file: {source}")
    return candidate


def _context_slice(root: Path, source: str, line: int) -> IsolatedContextSlice:
    path = _safe_source(root, source)
    try:
        text = read_utf8_text(path)
        raw = path.read_bytes()
    except (OSError, TextEncodingError) as exc:
        raise _fail("SDAI-ISOLATED-008", f"unable to read UTF-8 context source {source}: {exc}") from exc
    lines = text.splitlines()
    if line < 1 or line > max(1, len(lines)):
        raise _fail("SDAI-ISOLATED-008", f"context line {line} is outside {source}")
    start = max(1, line - _CONTEXT_WINDOW)
    end = min(len(lines), line + _CONTEXT_WINDOW)
    rendered = "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, end + 1))
    return IsolatedContextSlice(
        source=source,
        line_start=start,
        line_end=end,
        source_sha256=_hash_bytes(raw),
        text=rendered,
    )


def context_from_remediation(project_root: Path, task: RemediationTask) -> tuple[IsolatedContextSlice, ...]:
    root = project_root.resolve()
    by_location: dict[tuple[str, int], IsolatedContextSlice] = {}
    for provenance in task.provenance:
        key = (provenance.source, provenance.line)
        by_location[key] = _context_slice(root, provenance.source, provenance.line)
    result = tuple(by_location[key] for key in sorted(by_location, key=lambda item: (item[0].casefold(), item[0], item[1])))
    if sum(len(item.text) for item in result) > _MAX_CONTEXT_CHARS:
        raise _fail("SDAI-ISOLATED-008", "remediation provenance exceeds bounded context limit")
    return result


def _stage_dir(root: Path, feature_id: str, task_id: str, attempt: int, stage: IsolatedStage) -> Path:
    try:
        return ensure_within_project(
            root,
            root / ".sdai" / "isolated" / feature_id / task_id / f"attempt-{attempt}" / stage.value,
            label="isolated stage directory",
        )
    except PathSafetyError as exc:
        raise _fail("SDAI-ISOLATED-009", "isolated stage path escapes project root") from exc


def _contract_path(root: Path, contract: IsolatedTaskContract) -> Path:
    return _stage_dir(root, contract.feature_id, contract.task_id, contract.attempt, contract.stage) / "contract.json"


def _result_path(root: Path, result: IsolatedStageResult, feature_id: str, task_id: str, attempt: int) -> Path:
    return _stage_dir(root, feature_id, task_id, attempt, result.invocation.stage) / f"{result.invocation.invocation_id}.result.json"


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False)
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def persist_contract(project_root: Path, contract: IsolatedTaskContract) -> Path:
    root = project_root.resolve()
    path = _contract_path(root, contract)
    content = contract.to_json().encode("utf-8") + b"\n"
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise _fail("SDAI-ISOLATED-009", "isolated contract path is unsafe")
        if path.read_bytes() != content:
            raise _fail("SDAI-ISOLATED-009", "isolated contract already exists with different context/truth")
        return path
    _atomic_write(path, content)
    return path


def load_persisted_contract(
    project_root: Path,
    feature_id: str,
    task_id: str,
    attempt: int,
    stage: IsolatedStage,
) -> IsolatedTaskContract | None:
    root = project_root.resolve()
    path = _stage_dir(root, feature_id, task_id, attempt, stage) / "contract.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-ISOLATED-009", "persisted isolated contract path is unsafe")
    return IsolatedTaskContract.from_json(path.read_bytes())


def _attempt_and_dispatch(ledger: ExecutionLedger, task_id: str) -> tuple[int, str | None, bool]:
    attempt = 0
    dispatch: str | None = None
    registered = False
    for event in ledger.load_events():
        if event.task_id != task_id:
            continue
        if event.kind == "task.registered":
            attempt = 1
            dispatch = None
            registered = True
        elif event.kind == "task.reopened":
            attempt += 1
            dispatch = None
        elif event.kind == "task.dispatch_reserved":
            payload_attempt = event.payload.get("attempt")
            payload_dispatch = event.payload.get("dispatch_id")
            if payload_attempt == attempt and isinstance(payload_dispatch, str):
                dispatch = payload_dispatch
    return max(attempt, 1), dispatch, registered


def register_remediation_task(ledger: ExecutionLedger, task: RemediationTask) -> None:
    if ledger.manifest.feature_id != task.feature_id:
        raise _fail("SDAI-ISOLATED-010", "execution ledger feature does not match remediation task")
    attempt, _, registered = _attempt_and_dispatch(ledger, task.task_id)
    if not registered:
        ledger.append_event(
            "task.registered",
            task_id=task.task_id,
            payload={
                "source": "sdai.convergence-state/v1",
                "remediation_task_sha256": task.sha256,
                "verification_report_sha256": task.verification_report_sha256,
                "verification_input_sha256": task.verification_input_sha256,
                "round_id": task.round_id,
            },
        )
        attempt = 1
    brief = task.to_json() + "\n"
    path = ledger.task_record_paths(task.task_id)["brief"]
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != brief:
            raise _fail("SDAI-ISOLATED-010", "registered task brief differs from durable remediation contract")
    else:
        ledger.write_task_brief(task.task_id, brief)


def prepare_implementation_dispatch(ledger: ExecutionLedger, task: RemediationTask) -> IsolatedDispatch:
    register_remediation_task(ledger, task)
    attempt, existing, _ = _attempt_and_dispatch(ledger, task.task_id)
    if existing is not None:
        return IsolatedDispatch(existing, True, attempt)
    resumed = resume_execution(
        ledger.project_root,
        ledger.manifest.feature_id,
        ledger.manifest.run_id,
    )
    if resumed.status != "ready" or resumed.dispatch_id is None:
        raise _fail("SDAI-ISOLATED-010", f"unable to reserve isolated task dispatch: {resumed.status}")
    if resumed.plan.resume_task_id != task.task_id:
        raise _fail(
            "SDAI-ISOLATED-010",
            f"execution order requires task {resumed.plan.resume_task_id!r} before {task.task_id!r}",
        )
    selected = next(item for item in resumed.plan.tasks if item.task_id == task.task_id)
    return IsolatedDispatch(resumed.dispatch_id, resumed.dispatch_reused, selected.attempt)


def build_implementation_contract(
    project_root: Path,
    task: RemediationTask,
    dispatch: IsolatedDispatch,
) -> IsolatedTaskContract:
    root = project_root.resolve()
    existing = load_persisted_contract(root, task.feature_id, task.task_id, dispatch.attempt, IsolatedStage.IMPLEMENT)
    if existing is not None:
        if existing.dispatch_id != dispatch.dispatch_id or existing.remediation_task_sha256 != task.sha256:
            raise _fail("SDAI-ISOLATED-011", "persisted implementation contract does not match current dispatch/task")
        return existing
    agent, capability, mode = _STAGE_AGENT[IsolatedStage.IMPLEMENT]
    contract = IsolatedTaskContract(
        feature_id=task.feature_id,
        task_id=task.task_id,
        remediation_task_sha256=task.sha256,
        round_id=task.round_id,
        attempt=dispatch.attempt,
        stage=IsolatedStage.IMPLEMENT,
        git_commit=_head(root),
        dispatch_id=dispatch.dispatch_id,
        semantic_agent=agent,
        capability=capability,
        mode=mode,
        summary=task.summary,
        allowed_roots=task.allowed_roots,
        forbidden_roots=task.forbidden_roots,
        context=context_from_remediation(root, task),
    )
    persist_contract(root, contract)
    return contract


def _result_files(root: Path, feature_id: str, task_id: str, attempt: int, stage: IsolatedStage) -> tuple[Path, ...]:
    directory = _stage_dir(root, feature_id, task_id, attempt, stage)
    if not directory.exists():
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise _fail("SDAI-ISOLATED-009", "isolated stage result directory is unsafe")
    return tuple(sorted(directory.glob("*.result.json"), key=lambda path: path.name))


def _load_result(path: Path) -> IsolatedStageResult:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("SDAI-ISOLATED-009", f"invalid isolated result JSON: {exc}") from exc
    if not isinstance(raw, Mapping) or set(raw) != {"apiVersion", "invocation", "status", "git_commit", "output", "output_sha256", "sha256"}:
        raise _fail("SDAI-ISOLATED-007", "isolated result fields do not match contract")
    invocation_raw = raw["invocation"]
    if not isinstance(invocation_raw, Mapping) or invocation_raw.get("apiVersion") != ISOLATED_INVOCATION_API_VERSION:
        raise _fail("SDAI-ISOLATED-006", "isolated invocation fields/apiVersion do not match")
    record = IsolatedInvocationRecord(
        invocation_id=invocation_raw["invocation_id"],
        contract_sha256=invocation_raw["contract_sha256"],
        stage=invocation_raw["stage"],
        semantic_agent=invocation_raw["semantic_agent"],
        capability=invocation_raw["capability"],
        mode=invocation_raw["mode"],
        profile=invocation_raw["profile"],
        provider=invocation_raw["provider"],
        model=invocation_raw["model"],
        system_sha256=invocation_raw["system_sha256"],
        prompt_sha256=invocation_raw["prompt_sha256"],
    )
    result = IsolatedStageResult(record, raw["status"], raw["git_commit"], raw["output"])
    if raw["output_sha256"] != result.output_sha256 or raw["sha256"] != result.sha256:
        raise _fail("SDAI-ISOLATED-007", "isolated result hash mismatch")
    return result


def latest_stage_result(
    project_root: Path,
    feature_id: str,
    task_id: str,
    attempt: int,
    stage: IsolatedStage,
) -> IsolatedStageResult | None:
    root = project_root.resolve()
    files = _result_files(root, feature_id, task_id, attempt, stage)
    if not files:
        return None
    results = tuple(_load_result(path) for path in files)
    if len(results) > 1:
        identities = {item.invocation.invocation_id for item in results}
        if len(identities) > 1:
            raise _fail("SDAI-ISOLATED-009", f"multiple isolated invocations exist for stage {stage.value}")
    return results[-1]


def build_review_contract(
    project_root: Path,
    task: RemediationTask,
    implementation: IsolatedStageResult,
    stage: IsolatedStage,
    *,
    prior_review: IsolatedStageResult | None = None,
) -> IsolatedTaskContract:
    if stage not in {IsolatedStage.SPEC_COMPLIANCE_REVIEW, IsolatedStage.CODE_QUALITY_REVIEW}:
        raise _fail("SDAI-ISOLATED-011", "task review stage must be spec-compliance or code-quality")
    if implementation.invocation.stage is not IsolatedStage.IMPLEMENT:
        raise _fail("SDAI-ISOLATED-011", "review requires implementation-stage result")
    if implementation.status is not IsolatedStageStatus.PASSED:
        raise _fail("SDAI-ISOLATED-011", "review cannot start until implementation stage is recorded as passed")
    if implementation.invocation.semantic_agent == _STAGE_AGENT[stage][0]:
        raise _fail("SDAI-ISOLATED-011", "worker semantic agent cannot satisfy independent reviewer role")
    predecessor: tuple[str, ...] = ()
    if stage is IsolatedStage.CODE_QUALITY_REVIEW:
        if prior_review is None or prior_review.invocation.stage is not IsolatedStage.SPEC_COMPLIANCE_REVIEW:
            raise _fail("SDAI-ISOLATED-011", "code-quality review requires prior spec-compliance review")
        if prior_review.status is not IsolatedStageStatus.PASSED:
            raise _fail("SDAI-ISOLATED-011", "code-quality review requires passing spec-compliance review")
        predecessor = (prior_review.invocation.invocation_id,)
    root = project_root.resolve()
    attempt = implementation.invocation.stage and _attempt_from_result_path(root, task.feature_id, task.task_id, implementation)
    existing = load_persisted_contract(root, task.feature_id, task.task_id, attempt, stage)
    if existing is not None:
        return existing
    agent, capability, mode = _STAGE_AGENT[stage]
    contract = IsolatedTaskContract(
        feature_id=task.feature_id,
        task_id=task.task_id,
        remediation_task_sha256=task.sha256,
        round_id=task.round_id,
        attempt=attempt,
        stage=stage,
        git_commit=_head(root),
        dispatch_id=f"{implementation.invocation.invocation_id}:{stage.value}",
        semantic_agent=agent,
        capability=capability,
        mode=mode,
        summary=task.summary,
        allowed_roots=task.allowed_roots,
        forbidden_roots=task.forbidden_roots,
        context=context_from_remediation(root, task),
        predecessor_invocation_ids=predecessor,
        worker_invocation_id=implementation.invocation.invocation_id,
    )
    persist_contract(root, contract)
    return contract


def _attempt_from_result_path(
    root: Path,
    feature_id: str,
    task_id: str,
    result: IsolatedStageResult,
) -> int:
    base = root / ".sdai" / "isolated" / feature_id / task_id
    if not base.exists():
        raise _fail("SDAI-ISOLATED-009", "isolated result has no persisted task state")
    matches: list[int] = []
    for directory in base.glob("attempt-*"):
        try:
            attempt = int(directory.name.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        path = _result_path(root, result, feature_id, task_id, attempt)
        if path.exists():
            matches.append(attempt)
    if len(matches) != 1:
        raise _fail("SDAI-ISOLATED-009", "isolated result attempt is ambiguous or not persisted")
    return matches[0]


def build_isolated_invocation(
    runtime: AgentRuntime,
    contract: IsolatedTaskContract,
    *,
    profile_name: str | None = None,
) -> PreparedIsolatedInvocation:
    invocation = runtime.build_explicit_context_invocation(
        contract.feature_id,
        contract.capability,
        contract.prompt_context(),
        profile_name=profile_name,
        agent_name=contract.semantic_agent,
        mode=contract.mode,
    )
    if invocation.agent_name != contract.semantic_agent:
        raise _fail("SDAI-ISOLATED-012", "runtime resolved a different semantic agent than the task contract")
    seed = {
        "contract_sha256": contract.sha256,
        "stage": contract.stage.value,
        "agent_name": invocation.agent_name,
        "profile": invocation.profile.name,
        "provider": invocation.profile.provider,
        "model": invocation.profile.model,
        "system_sha256": _hash_bytes(invocation.system.encode("utf-8")),
        "prompt_sha256": _hash_bytes(invocation.prompt.encode("utf-8")),
    }
    invocation_id = "INVOCATION-" + sha256(_canonical_bytes(seed)).hexdigest()[:24]
    record = IsolatedInvocationRecord(
        invocation_id=invocation_id,
        contract_sha256=contract.sha256,
        stage=contract.stage,
        semantic_agent=contract.semantic_agent,
        capability=contract.capability,
        mode=contract.mode,
        profile=invocation.profile.name,
        provider=invocation.profile.provider,
        model=invocation.profile.model,
        system_sha256=seed["system_sha256"],
        prompt_sha256=seed["prompt_sha256"],
    )
    if contract.stage is not IsolatedStage.IMPLEMENT and record.invocation_id == contract.worker_invocation_id:
        raise _fail("SDAI-ISOLATED-012", "review invocation must be independent from worker invocation")
    return PreparedIsolatedInvocation(contract, invocation, record)


def execute_isolated_invocation(
    runtime: AgentRuntime,
    prepared: PreparedIsolatedInvocation,
    *,
    status: IsolatedStageStatus = IsolatedStageStatus.RECORDED,
) -> IsolatedStageResult:
    result: AgentExecutionResult = runtime.execute_invocation(prepared.invocation)
    if result.agent_name != prepared.record.semantic_agent:
        raise _fail("SDAI-ISOLATED-012", "provider execution returned unexpected semantic agent identity")
    return IsolatedStageResult(
        invocation=prepared.record,
        status=status,
        git_commit=_head(runtime.project_root.resolve()),
        output=result.output,
    )


def _ledger_event_for_result(
    ledger: ExecutionLedger,
    contract: IsolatedTaskContract,
    result: IsolatedStageResult,
    binding: HashBinding,
) -> None:
    kind = "task.implementation" if contract.stage is IsolatedStage.IMPLEMENT else "task.review"
    matching = [
        event
        for event in ledger.load_events()
        if event.task_id == contract.task_id
        and event.kind == kind
        and event.payload.get("invocation_id") == result.invocation.invocation_id
    ]
    if matching:
        event = matching[-1]
        if event.payload.get("result_sha256") != result.sha256:
            raise _fail("SDAI-ISOLATED-013", "existing ledger stage invocation has conflicting result")
        return
    ledger.append_event(
        kind,
        task_id=contract.task_id,
        git_commit=result.git_commit,
        bindings=(binding,),
        payload={
            "stage": contract.stage.value,
            "attempt": contract.attempt,
            "contract_sha256": contract.sha256,
            "invocation_id": result.invocation.invocation_id,
            "semantic_agent": result.invocation.semantic_agent,
            "status": result.status.value,
            "result_sha256": result.sha256,
            "worker_invocation_id": contract.worker_invocation_id,
            "predecessor_invocation_ids": list(contract.predecessor_invocation_ids),
        },
    )


def persist_stage_result(
    project_root: Path,
    contract: IsolatedTaskContract,
    result: IsolatedStageResult,
    *,
    ledger: ExecutionLedger | None = None,
) -> Path:
    root = project_root.resolve()
    if result.invocation.contract_sha256 != contract.sha256 or result.invocation.stage is not contract.stage:
        raise _fail("SDAI-ISOLATED-013", "isolated result does not belong to task contract")
    path = _result_path(root, result, contract.feature_id, contract.task_id, contract.attempt)
    content = result.to_json().encode("utf-8") + b"\n"
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise _fail("SDAI-ISOLATED-013", "isolated result path is unsafe")
        if path.read_bytes() != content:
            raise _fail("SDAI-ISOLATED-013", "same isolated invocation already has a conflicting result")
    else:
        _atomic_write(path, content)
    if ledger is not None:
        if ledger.manifest.feature_id != contract.feature_id:
            raise _fail("SDAI-ISOLATED-013", "execution ledger feature does not match isolated result")
        state = ledger.load_state()
        task_state = state.task_map().get(contract.task_id)
        if task_state is None:
            raise _fail("SDAI-ISOLATED-013", "isolated ledger task is not registered")
        if task_state.status == "registered":
            ledger.append_event(
                "task.started",
                task_id=contract.task_id,
                git_commit=contract.git_commit,
                payload={
                    "dispatch_id": contract.dispatch_id,
                    "attempt": contract.attempt,
                    "contract_sha256": contract.sha256,
                },
            )
        binding = ledger.binding_for_file(path, kind="evidence")
        _ledger_event_for_result(ledger, contract, result, binding)
    return path


def task_review_chain(
    project_root: Path,
    feature_id: str,
    task_id: str,
    attempt: int,
) -> tuple[IsolatedStageResult, ...]:
    root = project_root.resolve()
    result: list[IsolatedStageResult] = []
    for stage in (
        IsolatedStage.IMPLEMENT,
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
        IsolatedStage.CODE_QUALITY_REVIEW,
    ):
        item = latest_stage_result(root, feature_id, task_id, attempt, stage)
        if item is not None:
            result.append(item)
    return tuple(result)


def assert_task_individually_accepted(chain: Sequence[IsolatedStageResult]) -> None:
    by_stage = {item.invocation.stage: item for item in chain}
    for stage in (
        IsolatedStage.IMPLEMENT,
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
        IsolatedStage.CODE_QUALITY_REVIEW,
    ):
        item = by_stage.get(stage)
        if item is None or item.status is not IsolatedStageStatus.PASSED:
            raise _fail("SDAI-ISOLATED-014", f"task is not individually accepted; missing/pending {stage.value}")
    worker = by_stage[IsolatedStage.IMPLEMENT]
    for stage in (IsolatedStage.SPEC_COMPLIANCE_REVIEW, IsolatedStage.CODE_QUALITY_REVIEW):
        review = by_stage[stage]
        if review.invocation.semantic_agent == worker.invocation.semantic_agent:
            raise _fail("SDAI-ISOLATED-014", "worker semantic agent cannot approve its own task")
        if review.invocation.invocation_id == worker.invocation.invocation_id:
            raise _fail("SDAI-ISOLATED-014", "review invocation must be independent from worker invocation")


def build_final_change_review_contract(
    project_root: Path,
    feature_id: str,
    task_chains: Mapping[str, Sequence[IsolatedStageResult]],
    *,
    baseline_commit: str,
    attempt: int = 1,
) -> IsolatedTaskContract:
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    if not task_chains:
        raise _fail("SDAI-ISOLATED-015", "final change review requires at least one accepted task")
    worker_ids: list[str] = []
    predecessor_ids: list[str] = []
    summaries: list[str] = []
    aggregate: list[dict[str, object]] = []
    for task_id in sorted(task_chains):
        chain = tuple(task_chains[task_id])
        assert_task_individually_accepted(chain)
        by_stage = {item.invocation.stage: item for item in chain}
        worker = by_stage[IsolatedStage.IMPLEMENT]
        spec = by_stage[IsolatedStage.SPEC_COMPLIANCE_REVIEW]
        quality = by_stage[IsolatedStage.CODE_QUALITY_REVIEW]
        worker_ids.append(worker.invocation.invocation_id)
        predecessor_ids.extend([spec.invocation.invocation_id, quality.invocation.invocation_id])
        summaries.append(
            f"{task_id}: worker={worker.invocation.invocation_id}; "
            f"spec_review={spec.invocation.invocation_id}; code_review={quality.invocation.invocation_id}"
        )
        aggregate.append(
            {
                "task_id": task_id,
                "worker_result_sha256": worker.sha256,
                "spec_review_result_sha256": spec.sha256,
                "code_review_result_sha256": quality.sha256,
            }
        )
    baseline = _commit(baseline_commit)
    head = _head(root)
    diff = _git(root, "diff", "--no-ext-diff", "--unified=3", baseline, "--", ".")
    if len(diff) > _MAX_CONTEXT_CHARS:
        raise _fail("SDAI-ISOLATED-015", "whole-change Git diff exceeds final review context limit")
    diff_source = f".sdai/isolated/{feature}/final-change.diff"
    context = (
        IsolatedContextSlice(
            source=diff_source,
            line_start=1,
            line_end=max(1, len(diff.splitlines())),
            source_sha256=_hash_bytes(diff.encode("utf-8")),
            text=diff,
        ),
    )
    aggregate_hash = _hash({"accepted_tasks": aggregate, "baseline_commit": baseline, "head": head})
    agent, capability, mode = _STAGE_AGENT[IsolatedStage.FINAL_CHANGE_REVIEW]
    contract = IsolatedTaskContract(
        feature_id=feature,
        task_id="FINAL-CHANGE-REVIEW",
        remediation_task_sha256=aggregate_hash,
        round_id="FINAL-CHANGE",
        attempt=attempt,
        stage=IsolatedStage.FINAL_CHANGE_REVIEW,
        git_commit=head,
        dispatch_id=f"final:{aggregate_hash}",
        semantic_agent=agent,
        capability=capability,
        mode=mode,
        summary="Whole-change review after all remediation tasks passed independent spec and code-quality review.\n" + "\n".join(summaries),
        allowed_roots=(f".sdai/verification/{feature}/reviews",),
        forbidden_roots=(f"specs/changes/{feature}/requirements.md", "specs/current"),
        context=context,
        predecessor_invocation_ids=tuple(predecessor_ids),
        worker_invocation_id=worker_ids[0],
    )
    persist_contract(root, contract)
    return contract


__all__ = [
    "ISOLATED_INVOCATION_API_VERSION",
    "ISOLATED_RESULT_API_VERSION",
    "ISOLATED_TASK_API_VERSION",
    "IsolatedContextSlice",
    "IsolatedDispatch",
    "IsolatedInvocationRecord",
    "IsolatedStage",
    "IsolatedStageResult",
    "IsolatedStageStatus",
    "IsolatedTaskContract",
    "IsolatedTaskError",
    "PreparedIsolatedInvocation",
    "assert_task_individually_accepted",
    "build_final_change_review_contract",
    "build_implementation_contract",
    "build_isolated_invocation",
    "build_review_contract",
    "context_from_remediation",
    "execute_isolated_invocation",
    "latest_stage_result",
    "load_persisted_contract",
    "persist_contract",
    "persist_stage_result",
    "prepare_implementation_dispatch",
    "register_remediation_task",
    "task_review_chain",
]
