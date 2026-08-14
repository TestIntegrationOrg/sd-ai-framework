from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Callable, Mapping

from sdai.execution_guard import ProtectedPathViolation, WorkspaceMutationGuard
from sdai.integration_manifest import (
    IntegrationInputMode,
    IntegrationOutputMode,
)
from sdai.integration_registry import ResolvedIntegration
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.policy import EffectiveConfiguration
from sdai.providers.cli import build_provider_environment


INTEGRATION_EXECUTION_REQUEST_API_VERSION = "sdai.integration-execution-request/v1"
INTEGRATION_EXECUTION_PLAN_API_VERSION = "sdai.integration-execution-plan/v1"
INTEGRATION_EXECUTION_RESULT_API_VERSION = "sdai.integration-execution-result/v1"
INTEGRATION_EXECUTION_ERROR_API_VERSION = "sdai.integration-execution-error/v1"


class IntegrationExecutionError(RuntimeError):
    """Raised when an Integration cannot be planned or safely executed."""


class IntegrationExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    EXIT_ERROR = "exit-error"
    TIMED_OUT = "timed-out"
    CANCELLED = "cancelled"
    LAUNCH_ERROR = "launch-error"
    MALFORMED_OUTPUT = "malformed-output"
    POLICY_VIOLATION = "policy-violation"
    IO_ERROR = "io-error"


@dataclass
class CancellationToken:
    """Thread-safe cancellation signal for Integration execution."""

    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()


def _fail(code: str, message: str) -> IntegrationExecutionError:
    return IntegrationExecutionError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-INTEGRATION-EXEC-001", "execution data is not canonical finite JSON") from exc


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _input_bytes(value: str) -> bytes:
    if not isinstance(value, str):
        raise _fail("SDAI-INTEGRATION-EXEC-001", "Integration input must be a string")
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise _fail("SDAI-INTEGRATION-EXEC-001", "Integration input must be valid UTF-8 text") from exc


def _static_prefix(pattern: str) -> str:
    parts: list[str] = []
    for part in Path(pattern).parts:
        if any(char in part for char in "*?["):
            break
        parts.append(part)
    return Path(*parts).as_posix() if parts else "."


def _matches_protected_path(relative: str, patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(relative, pattern) or _static_prefix(pattern) == relative
        for pattern in patterns
    )


def _ensure_runtime_binding(
    resolved: ResolvedIntegration,
    request: "IntegrationExecutionRequest",
) -> None:
    if request.integration_identity != resolved.identity:
        raise _fail(
            "SDAI-INTEGRATION-EXEC-001",
            "execution request Integration identity does not match the resolved Integration",
        )
    if request.manifest_sha256 != resolved.manifest_sha256:
        raise _fail(
            "SDAI-INTEGRATION-EXEC-001",
            "execution request manifest hash does not match the resolved Integration",
        )


@dataclass(frozen=True)
class IntegrationExecutionRequest:
    integration_identity: str
    manifest_sha256: str
    input_sha256: str
    input_bytes: int
    _input_text: str = field(repr=False, compare=True)

    @classmethod
    def create(
        cls,
        resolved: ResolvedIntegration,
        input_text: str = "",
    ) -> "IntegrationExecutionRequest":
        payload = _input_bytes(input_text)
        return cls(
            integration_identity=resolved.identity,
            manifest_sha256=resolved.manifest_sha256,
            input_sha256=_sha256_bytes(payload),
            input_bytes=len(payload),
            _input_text=input_text,
        )

    @property
    def input_text(self) -> str:
        return self._input_text

    def as_dict(self) -> dict[str, object]:
        """Return the explainable request without serializing raw user input."""

        return {
            "apiVersion": INTEGRATION_EXECUTION_REQUEST_API_VERSION,
            "inputBytes": self.input_bytes,
            "inputSha256": self.input_sha256,
            "integrationIdentity": self.integration_identity,
            "manifestSha256": self.manifest_sha256,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


@dataclass(frozen=True)
class IntegrationExecutionPlan:
    integration_identity: str
    integration_version: str
    manifest_sha256: str
    executable: str
    args_before_input: tuple[str, ...]
    input_mode: IntegrationInputMode
    input_path: str | None
    input_sha256: str
    input_bytes: int
    args_after_input: tuple[str, ...]
    output_mode: IntegrationOutputMode
    output_path: str | None
    timeout_seconds: int
    environment_names: tuple[str, ...]
    requires_network: bool
    requires_workspace_write: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": INTEGRATION_EXECUTION_PLAN_API_VERSION,
            "argsAfterInput": list(self.args_after_input),
            "argsBeforeInput": list(self.args_before_input),
            "environmentNames": list(self.environment_names),
            "executable": self.executable,
            "inputBytes": self.input_bytes,
            "inputMode": self.input_mode.value,
            "inputPath": self.input_path,
            "inputSha256": self.input_sha256,
            "integrationIdentity": self.integration_identity,
            "integrationVersion": self.integration_version,
            "manifestSha256": self.manifest_sha256,
            "outputMode": self.output_mode.value,
            "outputPath": self.output_path,
            "requiresNetwork": self.requires_network,
            "requiresWorkspaceWrite": self.requires_workspace_write,
            "timeoutSeconds": self.timeout_seconds,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _sha256_bytes(self.to_json().encode("utf-8"))

    def runtime_argv(self, request: IntegrationExecutionRequest) -> tuple[str, ...]:
        if request.integration_identity != self.integration_identity:
            raise _fail("SDAI-INTEGRATION-EXEC-001", "request identity does not match execution plan")
        if request.manifest_sha256 != self.manifest_sha256:
            raise _fail("SDAI-INTEGRATION-EXEC-001", "request manifest hash does not match execution plan")
        if request.input_sha256 != self.input_sha256 or request.input_bytes != self.input_bytes:
            raise _fail("SDAI-INTEGRATION-EXEC-001", "request input does not match execution plan")

        argv = [self.executable, *self.args_before_input]
        if self.input_mode == IntegrationInputMode.ARGUMENT:
            argv.append(request.input_text)
        elif self.input_mode == IntegrationInputMode.FILE:
            assert self.input_path is not None
            argv.append(self.input_path)
        argv.extend(self.args_after_input)
        return tuple(argv)


@dataclass(frozen=True)
class IntegrationExecutionErrorRecord:
    code: str
    category: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": INTEGRATION_EXECUTION_ERROR_API_VERSION,
            "category": self.category,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class IntegrationExecutionResult:
    integration_identity: str
    manifest_sha256: str
    plan_sha256: str
    status: IntegrationExecutionStatus
    exit_code: int | None
    output: object | None
    error: IntegrationExecutionErrorRecord | None

    @property
    def succeeded(self) -> bool:
        return self.status == IntegrationExecutionStatus.SUCCEEDED

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": INTEGRATION_EXECUTION_RESULT_API_VERSION,
            "error": None if self.error is None else self.error.as_dict(),
            "exitCode": self.exit_code,
            "integrationIdentity": self.integration_identity,
            "manifestSha256": self.manifest_sha256,
            "output": self.output,
            "planSha256": self.plan_sha256,
            "status": self.status.value,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


def build_integration_execution_plan(
    resolved: ResolvedIntegration,
    request: IntegrationExecutionRequest,
    policy: EffectiveConfiguration,
) -> IntegrationExecutionPlan:
    """Build an explainable plan while keeping raw input and environment values private."""

    _ensure_runtime_binding(resolved, request)
    execution = resolved.manifest.execution
    if execution is None:
        raise _fail(
            "SDAI-INTEGRATION-EXEC-001",
            f"Integration '{resolved.identity}' does not declare agent execution",
        )

    if execution.input_mode == IntegrationInputMode.NONE and request.input_bytes:
        raise _fail(
            "SDAI-INTEGRATION-EXEC-001",
            "inputMode 'none' does not accept runtime input",
        )

    if resolved.manifest.security.requires_workspace_write and not policy.workspace_write:
        raise _fail(
            "SDAI-INTEGRATION-EXEC-002",
            "Integration requires workspace-write but effective SDAI policy disables it",
        )

    requested_environment = tuple(resolved.manifest.security.environment)
    if policy.environment_allowlist is not None:
        blocked = sorted(set(requested_environment) - set(policy.environment_allowlist))
        if blocked:
            raise _fail(
                "SDAI-INTEGRATION-EXEC-002",
                "Integration requires environment variable name(s) not allowed by effective SDAI policy: "
                + ", ".join(blocked),
            )

    for label, relative in (
        ("inputPath", execution.input_path),
        ("outputPath", execution.output_path),
    ):
        if relative is not None and _matches_protected_path(relative, policy.protected_paths):
            raise _fail(
                "SDAI-INTEGRATION-EXEC-002",
                f"execution.{label} '{relative}' overlaps an SDAI protected path",
            )

    return IntegrationExecutionPlan(
        integration_identity=resolved.identity,
        integration_version=str(resolved.version),
        manifest_sha256=resolved.manifest_sha256,
        executable=execution.executable,
        args_before_input=execution.args_before_input,
        input_mode=execution.input_mode,
        input_path=execution.input_path,
        input_sha256=request.input_sha256,
        input_bytes=request.input_bytes,
        args_after_input=execution.args_after_input,
        output_mode=execution.output_mode,
        output_path=execution.output_path,
        timeout_seconds=execution.timeout_seconds,
        environment_names=requested_environment,
        requires_network=resolved.manifest.security.requires_network,
        requires_workspace_write=resolved.manifest.security.requires_workspace_write,
    )


def _runtime_path(project_root: Path, relative: str, *, label: str) -> Path:
    root = project_root.resolve()
    candidate = root / Path(relative)
    try:
        ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-INTEGRATION-EXEC-003", f"{label} escapes the project workspace") from exc

    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise _fail(
                "SDAI-INTEGRATION-EXEC-003",
                f"{label} must not contain symlink components",
            )
    return candidate


def _mkdir_runtime_parents(path: Path, project_root: Path) -> list[Path]:
    root = project_root.resolve()
    missing: list[Path] = []
    current = path.parent
    while current != root and not current.exists():
        missing.append(current)
        current = current.parent
    if current.is_symlink():
        raise _fail("SDAI-INTEGRATION-EXEC-003", "runtime path parent must not be a symlink")
    for directory in reversed(missing):
        directory.mkdir()
    return missing


def _cleanup_runtime_path(path: Path | None, created_dirs: list[Path]) -> None:
    if path is not None:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
        except OSError:
            pass
    for directory in created_dirs:
        try:
            directory.rmdir()
        except OSError:
            pass


def _resolve_executable(project_root: Path, executable: str) -> str:
    if "/" not in executable:
        return executable
    path = _runtime_path(project_root, executable, label="Integration executable")
    if path.is_symlink() or not path.is_file():
        raise _fail(
            "SDAI-INTEGRATION-EXEC-004",
            f"project-relative Integration executable '{executable}' is not a regular file",
        )
    return str(path)


def _decode_utf8(data: bytes, *, stream: str) -> str:
    try:
        return data.decode("utf-8", errors="strict").removeprefix("\ufeff")
    except UnicodeDecodeError as exc:
        raise _fail(
            "SDAI-INTEGRATION-EXEC-008",
            f"Integration returned invalid UTF-8 on {stream} at byte {exc.start}",
        ) from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key '{key}'")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant '{value}'")


def _normalize_output(mode: IntegrationOutputMode, data: bytes) -> object | None:
    if mode == IntegrationOutputMode.NONE:
        return None
    stream = "stderr" if mode in {IntegrationOutputMode.STDERR, IntegrationOutputMode.JSON_STDERR} else "stdout"
    text = _decode_utf8(data, stream=stream).strip()
    if mode in {IntegrationOutputMode.JSON_STDOUT, IntegrationOutputMode.JSON_STDERR}:
        if not text:
            raise _fail("SDAI-INTEGRATION-EXEC-008", f"Integration returned empty JSON on {stream}")
        try:
            value = json.loads(
                text,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
            _canonical_json(value)
            return value
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise _fail("SDAI-INTEGRATION-EXEC-008", f"Integration returned malformed JSON on {stream}") from exc
    return text


def _terminate_process(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        return process.communicate(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        return process.communicate()


def _communicate_with_control(
    process: subprocess.Popen[bytes],
    *,
    stdin_bytes: bytes | None,
    timeout_seconds: int,
    cancellation: CancellationToken | None,
) -> tuple[str, bytes, bytes]:
    if stdin_bytes is not None and process.stdin is not None:
        try:
            process.stdin.write(stdin_bytes)
            process.stdin.close()
            process.stdin = None
        except (BrokenPipeError, OSError):
            process.stdin = None

    deadline = time.monotonic() + timeout_seconds
    while True:
        if cancellation is not None and cancellation.is_cancelled:
            stdout, stderr = _terminate_process(process)
            return "cancelled", stdout or b"", stderr or b""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stdout, stderr = _terminate_process(process)
            return "timed-out", stdout or b"", stderr or b""
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            return "completed", stdout or b"", stderr or b""
        except subprocess.TimeoutExpired:
            continue


def _error_result(
    plan: IntegrationExecutionPlan,
    *,
    status: IntegrationExecutionStatus,
    code: str,
    category: str,
    message: str,
    exit_code: int | None = None,
) -> IntegrationExecutionResult:
    return IntegrationExecutionResult(
        integration_identity=plan.integration_identity,
        manifest_sha256=plan.manifest_sha256,
        plan_sha256=plan.sha256,
        status=status,
        exit_code=exit_code,
        output=None,
        error=IntegrationExecutionErrorRecord(code=code, category=category, message=message),
    )


def execute_integration_plan(
    plan: IntegrationExecutionPlan,
    request: IntegrationExecutionRequest,
    *,
    project_root: Path,
    policy: EffectiveConfiguration,
    cancellation: CancellationToken | None = None,
    environment: Mapping[str, str] | None = None,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> IntegrationExecutionResult:
    """Execute one policy-checked Integration plan with direct argv subprocess semantics.

    Raw user input and environment values are runtime-only. They never appear in the
    canonical plan or result. The function revalidates policy immediately before launch,
    so a stale plan cannot bypass a newly tightened environment/workspace policy.
    """

    if request.integration_identity != plan.integration_identity or request.manifest_sha256 != plan.manifest_sha256:
        raise _fail("SDAI-INTEGRATION-EXEC-001", "request does not match Integration execution plan")
    if request.input_sha256 != plan.input_sha256 or request.input_bytes != plan.input_bytes:
        raise _fail("SDAI-INTEGRATION-EXEC-001", "request input does not match Integration execution plan")

    if plan.requires_workspace_write and not policy.workspace_write:
        raise _fail("SDAI-INTEGRATION-EXEC-002", "effective SDAI policy no longer permits workspace-write")
    if policy.environment_allowlist is not None:
        blocked = sorted(set(plan.environment_names) - set(policy.environment_allowlist))
        if blocked:
            raise _fail(
                "SDAI-INTEGRATION-EXEC-002",
                "effective SDAI policy no longer permits Integration environment name(s): " + ", ".join(blocked),
            )

    root = project_root.resolve()
    if not root.is_dir():
        raise _fail("SDAI-INTEGRATION-EXEC-003", "project root must be an existing directory")

    if cancellation is not None and cancellation.is_cancelled:
        return _error_result(
            plan,
            status=IntegrationExecutionStatus.CANCELLED,
            code="SDAI-INTEGRATION-EXEC-006",
            category="cancelled",
            message="Integration execution was cancelled before launch",
        )

    input_path: Path | None = None
    output_path: Path | None = None
    input_dirs: list[Path] = []
    output_dirs: list[Path] = []

    try:
        if plan.input_path is not None:
            input_path = _runtime_path(root, plan.input_path, label="Integration input file")
            if input_path.exists() or input_path.is_symlink():
                raise _fail("SDAI-INTEGRATION-EXEC-003", "Integration input file already exists")
            input_dirs = _mkdir_runtime_parents(input_path, root)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(input_path, flags, 0o600)
            try:
                os.write(fd, _input_bytes(request.input_text))
            finally:
                os.close(fd)

        if plan.output_path is not None:
            output_path = _runtime_path(root, plan.output_path, label="Integration output file")
            if output_path.exists() or output_path.is_symlink():
                raise _fail("SDAI-INTEGRATION-EXEC-003", "Integration output file already exists")
            output_dirs = _mkdir_runtime_parents(output_path, root)

        argv = list(plan.runtime_argv(request))
        argv[0] = _resolve_executable(root, argv[0])
        stdin_bytes = _input_bytes(request.input_text) if plan.input_mode == IntegrationInputMode.STDIN else None

        if environment is None:
            runtime_environment = build_provider_environment(
                "integration",
                profile_allowlist=plan.environment_names,
                policy_allowlist=policy.environment_allowlist,
            )
        else:
            allowed_names = set(plan.environment_names)
            if policy.environment_allowlist is not None:
                allowed_names.intersection_update(policy.environment_allowlist)
            runtime_environment = {
                name: value
                for name, value in environment.items()
                if name in allowed_names or name in {"PATH", "HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR", "SYSTEMROOT", "WINDIR", "PATHEXT"}
            }

        try:
            with WorkspaceMutationGuard(root, policy.protected_paths):
                process = popen_factory(
                    argv,
                    cwd=root,
                    stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=runtime_environment,
                    shell=False,
                )
                control, stdout, stderr = _communicate_with_control(
                    process,
                    stdin_bytes=stdin_bytes,
                    timeout_seconds=plan.timeout_seconds,
                    cancellation=cancellation,
                )
        except ProtectedPathViolation:
            return _error_result(
                plan,
                status=IntegrationExecutionStatus.POLICY_VIOLATION,
                code="SDAI-INTEGRATION-EXEC-009",
                category="policy",
                message="Integration modified protected SDAI/source-of-truth paths; changes were restored",
            )
        except (OSError, ValueError) as exc:
            return _error_result(
                plan,
                status=IntegrationExecutionStatus.LAUNCH_ERROR,
                code="SDAI-INTEGRATION-EXEC-004",
                category="launch",
                message=f"Integration process could not be launched: {exc.__class__.__name__}",
            )

        if control == "cancelled":
            return _error_result(
                plan,
                status=IntegrationExecutionStatus.CANCELLED,
                code="SDAI-INTEGRATION-EXEC-006",
                category="cancelled",
                message="Integration execution was cancelled",
            )
        if control == "timed-out":
            return _error_result(
                plan,
                status=IntegrationExecutionStatus.TIMED_OUT,
                code="SDAI-INTEGRATION-EXEC-005",
                category="timeout",
                message=f"Integration execution exceeded {plan.timeout_seconds} seconds",
            )

        exit_code = process.returncode
        if exit_code != 0:
            return _error_result(
                plan,
                status=IntegrationExecutionStatus.EXIT_ERROR,
                code="SDAI-INTEGRATION-EXEC-007",
                category="exit",
                message="Integration process exited with a non-zero status",
                exit_code=exit_code,
            )

        try:
            if plan.output_mode == IntegrationOutputMode.FILE:
                assert output_path is not None
                if output_path.is_symlink() or not output_path.is_file():
                    raise _fail("SDAI-INTEGRATION-EXEC-008", "Integration output file was not produced as a regular file")
                data = output_path.read_bytes()
                output = _normalize_output(IntegrationOutputMode.STDOUT, data)
            elif plan.output_mode in {IntegrationOutputMode.STDERR, IntegrationOutputMode.JSON_STDERR}:
                output = _normalize_output(plan.output_mode, stderr)
            else:
                output = _normalize_output(plan.output_mode, stdout)
        except (IntegrationExecutionError, OSError) as exc:
            return _error_result(
                plan,
                status=IntegrationExecutionStatus.MALFORMED_OUTPUT,
                code="SDAI-INTEGRATION-EXEC-008",
                category="output",
                message=str(exc).split(": ", 1)[-1],
                exit_code=exit_code,
            )

        return IntegrationExecutionResult(
            integration_identity=plan.integration_identity,
            manifest_sha256=plan.manifest_sha256,
            plan_sha256=plan.sha256,
            status=IntegrationExecutionStatus.SUCCEEDED,
            exit_code=exit_code,
            output=output,
            error=None,
        )
    except IntegrationExecutionError as exc:
        return _error_result(
            plan,
            status=IntegrationExecutionStatus.IO_ERROR,
            code="SDAI-INTEGRATION-EXEC-003",
            category="io",
            message=str(exc).split(": ", 1)[-1],
        )
    except OSError as exc:
        return _error_result(
            plan,
            status=IntegrationExecutionStatus.IO_ERROR,
            code="SDAI-INTEGRATION-EXEC-003",
            category="io",
            message=f"Integration runtime file operation failed: {exc.__class__.__name__}",
        )
    finally:
        _cleanup_runtime_path(output_path, output_dirs)
        _cleanup_runtime_path(input_path, input_dirs)
