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
    # Codex exec supports reading the task from stdin and exposes explicit sandbox modes.
    return CliProvider(command, cwd=cwd, timeout_seconds=timeout_seconds, provider_name="codex")


def copilot_provider(
    *, cwd: Path, model: str | None, timeout_seconds: int, extra_args: tuple[str, ...], mode: ExecutionMode
) -> CliProvider:
    # Copilot CLI accepts piped prompts; stdin avoids OS command-line length limits.
    command = ["copilot", "-s", "--no-ask-user"]
    if mode == ExecutionMode.ADVISORY:
        # Do not rely on prompts or persisted approvals for advisory safety. Restrict the
        # model to read-only repository tools and explicitly deny write/shell tools.
        command.extend(
            [
                "--plan",
                "--available-tools=view,grep,glob",
                "--deny-tool=write",
                "--deny-tool=shell",
            ]
        )
    else:
        command.append("--allow-tool=write")
    _append_model(command, model)
    command.extend(extra_args)
    return CliProvider(command, cwd=cwd, timeout_seconds=timeout_seconds, provider_name="copilot")


def claude_provider(
    *, cwd: Path, model: str | None, timeout_seconds: int, extra_args: tuple[str, ...], mode: ExecutionMode
) -> CliProvider:
    # Claude Code supports piped context in print mode. Pin permission mode so saved
    # local settings cannot silently widen advisory access.
    permission_mode = "acceptEdits" if mode == ExecutionMode.WORKSPACE_WRITE else "plan"
    command = [
        "claude",
        "-p",
        "Follow the SD-AI task supplied on stdin.",
        "--output-format",
        "text",
        "--no-session-persistence",
        "--permission-mode",
        permission_mode,
    ]
    _append_model(command, model)
    command.extend(extra_args)
    return CliProvider(command, cwd=cwd, timeout_seconds=timeout_seconds, provider_name="claude")


def gemini_provider(
    *, cwd: Path, model: str | None, timeout_seconds: int, extra_args: tuple[str, ...], mode: ExecutionMode
) -> CliProvider:
    # Gemini CLI appends piped stdin to the non-interactive prompt. Use Plan Mode for
    # advisory runs and auto_edit only after the caller explicitly selects workspace-write.
    approval_mode = "auto_edit" if mode == ExecutionMode.WORKSPACE_WRITE else "plan"
    command = [
        "gemini",
        "-p",
        "Follow the SD-AI task supplied on stdin.",
        "--output-format",
        "text",
        "--approval-mode",
        approval_mode,
    ]
    if model:
        command.extend(["-m", model])
    command.extend(extra_args)
    return CliProvider(command, cwd=cwd, timeout_seconds=timeout_seconds, provider_name="gemini")
