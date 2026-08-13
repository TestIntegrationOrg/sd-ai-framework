from __future__ import annotations

import codecs
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
_DEFAULT_MAX_SNAPSHOT_CHARS = 60_000
_READ_CHUNK = 64 * 1024


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


def _is_framework_state(source: str) -> bool:
    return source.startswith(".sdai/") or "/.sdai/" in source


def _untracked_entries(
    root: Path,
    *,
    excluded_prefixes: tuple[str, ...],
) -> tuple[tuple[str, Path, int], ...]:
    raw = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", binary=True)
    assert isinstance(raw, bytes)
    entries: list[tuple[str, Path, int]] = []
    for item in raw.split(b"\x00"):
        if not item:
            continue
        source = _portable_source(item)
        if _is_framework_state(source) or any(
            source == prefix.rstrip("/") or source.startswith(prefix)
            for prefix in excluded_prefixes
        ):
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
            size = path.stat().st_size
        except OSError as exc:
            raise _fail(f"unable to stat untracked review input {source}: {exc}") from exc
        entries.append((source, path, size))
    return tuple(sorted(entries, key=lambda item: (item[0].casefold(), item[0])))


def _render_untracked(source: str, path: Path, size: int, *, max_text_chars: int) -> str:
    """Render one untracked file without buffering unbounded content in memory."""

    digest = sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    text_parts: list[str] = []
    char_count = 0
    binary = False
    too_large_text = False

    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                if binary:
                    continue
                if b"\x00" in chunk:
                    binary = True
                    text_parts.clear()
                    continue
                try:
                    decoded = decoder.decode(chunk, final=False)
                except UnicodeDecodeError:
                    binary = True
                    text_parts.clear()
                    continue
                char_count += len(decoded)
                if char_count <= max_text_chars and not too_large_text:
                    text_parts.append(decoded)
                else:
                    too_large_text = True
                    text_parts.clear()
            if not binary:
                try:
                    tail = decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    binary = True
                    text_parts.clear()
                else:
                    char_count += len(tail)
                    if char_count <= max_text_chars and not too_large_text:
                        text_parts.append(tail)
                    elif tail or char_count > max_text_chars:
                        too_large_text = True
                        text_parts.clear()
    except OSError as exc:
        raise _fail(f"unable to stream untracked review input {source}: {exc}") from exc

    digest_text = "sha256:" + digest.hexdigest()
    if binary:
        return (
            f"SDAI-UNTRACKED-BINARY {source}\n"
            f"sha256={digest_text}\n"
            f"size={size}\n"
        )
    if too_large_text:
        raise _fail(
            f"untracked text review input exceeds remaining snapshot budget: {source}"
        )

    text = "".join(text_parts)
    return (
        f"SDAI-UNTRACKED-TEXT {source}\n"
        f"sha256={digest_text}\n"
        f"size={size}\n"
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
    max_chars: int = _DEFAULT_MAX_SNAPSHOT_CHARS,
) -> str:
    """Render bounded deterministic tracked + untracked review truth.

    Git's ordinary diff omits untracked files. This snapshot appends their exact byte
    identity (and UTF-8 contents when textual). Framework-owned ``.sdai`` execution/
    review state is excluded so the snapshot does not recursively include its own
    durable bookkeeping. Untracked files are streamed and text buffering is bounded.
    """

    root = project_root.resolve()
    commit = base_commit.strip().casefold() if isinstance(base_commit, str) else ""
    if not _GIT_COMMIT.fullmatch(commit):
        raise _fail(f"invalid snapshot base commit: {base_commit!r}")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise _fail("snapshot max_chars must be a positive integer")

    tracked = _run_git(root, "diff", "--no-ext-diff", "--unified=3", commit, "--", ".")
    assert isinstance(tracked, str)
    tracked_text = tracked.rstrip("\n")
    if len(tracked_text) > max_chars:
        raise _fail("tracked workspace diff exceeds snapshot budget")

    parts: list[str] = []
    used = 0
    if tracked_text:
        parts.append(tracked_text)
        used = len(tracked_text)

    for source, path, size in _untracked_entries(root, excluded_prefixes=excluded_prefixes):
        separator = 1 if parts else 0
        remaining = max_chars - used - separator
        if remaining <= 0:
            raise _fail("workspace snapshot exceeds configured budget")
        rendered = _render_untracked(
            source,
            path,
            size,
            max_text_chars=remaining,
        ).strip("\n")
        projected = used + separator + len(rendered)
        if projected > max_chars:
            raise _fail(f"untracked review input exceeds snapshot budget: {source}")
        parts.append(rendered)
        used = projected

    return ("\n".join(parts).rstrip("\n") + "\n") if parts else ""


__all__ = [
    "IsolatedWorkspaceError",
    "current_head",
    "render_workspace_snapshot",
]
