from __future__ import annotations

from pathlib import Path

from sdai.agent_platform.models import ExecutionMode
from sdai.providers.cli import CliProvider


def _append_model(command: list[str], model: str | None, flag: str = "--model") -> None:
    if model:
        command.extend([flag, model])


def codex_provider(
    *, cwd: Path, model: str | None, timeout_seconds: int, extra_args: tuple[str, ...], mode: ExecutionMode
) -> CliProvider:
    sandbox = "workspace-write" if mode == ExecutionMode.WORKSPACE_WRITE else "read-only"
    command = ["codex", "exec", "--ephemeral", "--sandbox", sandbox]
    _append_model(command, model)
    command.extend(extra_args)
    # Codex exec officially supports reading the task from stdin.
    return CliProvider(command, cwd=cwd, timeout_seconds=timeout_seconds, provider_name="codex")


def copilot_provider(
    *, cwd: Path, model: str | None, timeout_seconds: int, extra_args: tuple[str, ...], mode: ExecutionMode
) -> CliProvider:
    # Copilot CLI supports piped prompts; using stdin avoids OS command-line length limits.
    command = ["copilot", "-s", "--no-ask-user"]
    if mode == ExecutionMode.WORKSPACE_WRITE:
        command.append("--allow-tool=write")
    _append_model(command, model)
    command.extend(extra_args)
    return CliProvider(command, cwd=cwd, timeout_seconds=timeout_seconds, provider_name="copilot")


def claude_provider(
    *, cwd: Path, model: str | None, timeout_seconds: int, extra_args: tuple[str, ...], mode: ExecutionMode
) -> CliProvider:
    # Claude Code supports piped context in print mode. Keep the argument prompt short.
    command = ["claude", "-p", "Follow the SD-AI task supplied on stdin.", "--output-format", "text"]
    _append_model(command, model)
    command.extend(extra_args)
    return CliProvider(command, cwd=cwd, timeout_seconds=timeout_seconds, provider_name="claude")


def gemini_provider(
    *, cwd: Path, model: str | None, timeout_seconds: int, extra_args: tuple[str, ...], mode: ExecutionMode
) -> CliProvider:
    # Gemini CLI appends piped stdin to the non-interactive prompt.
    command = ["gemini", "-p", "Follow the SD-AI task supplied on stdin.", "--output-format", "text"]
    if model:
        command.extend(["-m", model])
    command.extend(extra_args)
    return CliProvider(command, cwd=cwd, timeout_seconds=timeout_seconds, provider_name="gemini")
