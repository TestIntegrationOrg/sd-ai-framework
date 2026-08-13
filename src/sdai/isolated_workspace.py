from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess

from sdai.path_safety import PathSafetyError, ensure_within_project


class IsolatedWorkspaceError(RuntimeError):
    """Raised when an isolated review workspace snapshot cannot be captured safely."""


_GIT_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DEFAULT_EXCLUDED_PREFIXES = (".sdai/isolated/",)


def _fail(message: str) -> IsolatedWorkspaceError:
    return IsolatedWorkspaceError(f"SDAI-ISOLATED-020: {message}")


def _git_executable() -> str:
    candidate = shutil.which("git")
    if not candidate:
        raise _fail("Git executable is unavailable")
    resolved = Path(candidate).resolve()
    if not resolved.is_file():
        raise _fail("resolved Git executable is not a regular file")
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


def _run_git(root: Path, *args: str, binary: bool = False) -> bytes | str:
    completed = subprocess.run(
        [_git_executable(), *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "strict",
        shell=False,
        check=False,
        env=_git_env(),
    )
    if completed.returncode != 0:
        stderr = completed.stderr
        stdout = completed.stdout
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", errors="replace").strip()
        else:
            detail = (stderr or "").strip()
        if not detail:
            if isinstance(stdout, bytes):
                detail = stdout.decode("utf-8", errors="replace").strip()
            else:
                detail = (stdout or "").strip()
        raise _fail(f"git {' '.join(args)} failed: {detail or f'exit {completed.returncode}'}")
    return completed.stdout


def current_head(project_root: Path) -> str:
    root = project_root.resolve()
    raw = _run_git(root, "rev-parse", "--verify", "HEAD")
    assert isinstance(raw, str)
    commit = raw.strip().casefold()
    if not _GIT_COMMIT.fullmatch(commit):
        raise _fail("Git HEAD is not a canonical commit identity")
    return commit


def _portable_source(raw: bytes) -> str:
    try:
        source = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail("untracked path is not valid UTF-8") from exc
    if not source or "\\" in source or source.startswith("/") or re.match(r"^[A-Za-z]:", source):
        raise _fail(f"untracked path is not repository-relative POSIX text: {source!r}")
    path = PurePosixPath(source)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise _fail(f"untracked path contains an unsafe segment: {source!r}")
    return path.as_posix()


def _untracked_entries(
    root: Path,
    *,
    excluded_prefixes: tuple[str, ...],
) -> tuple[tuple[str, bytes], ...]:
    raw = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", binary=True)
    assert isinstance(raw, bytes)
    entries: list[tuple[str, bytes]] = []
    for item in raw.split(b"\x00"):
        if not item:
            continue
        source = _portable_source(item)
        if any(source == prefix.rstrip("/") or source.startswith(prefix) for prefix in excluded_prefixes):
            continue
        try:
            path = ensure_within_project(
                root,
                root.joinpath(*PurePosixPath(source).parts),
                label="isolated review untracked path",
            )
        except PathSafetyError as exc:
            raise _fail(f"untracked path escapes project root: {source}") from exc
        if path.is_symlink() or not path.is_file():
            raise _fail(f"untracked review input must be a regular non-symlink file: {source}")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise _fail(f"unable to read untracked review input {source}: {exc}") from exc
        entries.append((source, content))
    return tuple(sorted(entries, key=lambda item: (item[0].casefold(), item[0])))


def _render_untracked(source: str, content: bytes) -> str:
    digest = "sha256:" + sha256(content).hexdigest()
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return (
            f"\nSDAI-UNTRACKED-BINARY {source}\n"
            f"sha256={digest}\n"
            f"size={len(content)}\n"
        )
    return (
        f"\nSDAI-UNTRACKED-TEXT {source}\n"
        f"sha256={digest}\n"
        f"size={len(content)}\n"
        "--- /dev/null\n"
        f"+++ b/{source}\n"
        "@@ SDAI-UNTRACKED @@\n"
        f"{text}"
        + ("\n" if text and not text.endswith("\n") else "")
    )


def render_workspace_snapshot(
    project_root: Path,
    base_commit: str,
    *,
    excluded_prefixes: tuple[str, ...] = _DEFAULT_EXCLUDED_PREFIXES,
) -> str:
    """Render deterministic tracked + untracked review truth since ``base_commit``.

    Git's ordinary diff omits untracked files. This snapshot appends their exact byte
    identity (and UTF-8 contents when textual) while excluding framework-generated
    ``.sdai/isolated/**`` review material so the review snapshot does not include itself.
    """

    root = project_root.resolve()
    commit = base_commit.strip().casefold() if isinstance(base_commit, str) else ""
    if not _GIT_COMMIT.fullmatch(commit):
        raise _fail(f"invalid snapshot base commit: {base_commit!r}")
    tracked = _run_git(root, "diff", "--no-ext-diff", "--unified=3", commit, "--", ".")
    assert isinstance(tracked, str)
    parts: list[str] = []
    if tracked:
        parts.append(tracked.rstrip("\n"))
    for source, content in _untracked_entries(root, excluded_prefixes=excluded_prefixes):
        parts.append(_render_untracked(source, content).strip("\n"))
    return ("\n".join(parts).rstrip("\n") + "\n") if parts else ""


__all__ = [
    "IsolatedWorkspaceError",
    "current_head",
    "render_workspace_snapshot",
]
