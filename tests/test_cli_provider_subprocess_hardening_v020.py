from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from threading import Timer

import pytest

from sdai.providers.cli import (
    CliProvider,
    ProviderEncodingError,
    ProviderExecutionError,
    ProviderOutputLimitError,
    ProviderStartupError,
)
from sdai.providers.control import ProviderCancellationToken, ProviderCancelledError


def _provider(tmp_path: Path, code: str, **kwargs) -> CliProvider:
    return CliProvider(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        provider_name="test-cli",
        timeout_seconds=kwargs.pop("timeout_seconds", 5),
        heartbeat_interval_seconds=kwargs.pop("heartbeat_interval_seconds", 0.05),
        poll_interval_seconds=kwargs.pop("poll_interval_seconds", 0.01),
        **kwargs,
    )


def test_unicode_workspace_prompt_and_output_are_strict_utf8(tmp_path: Path) -> None:
    root = tmp_path / "workspace café 日本 🚀"
    root.mkdir()
    provider = _provider(
        root,
        "import os,sys; data=sys.stdin.buffer.read(); text=data.decode('utf-8'); "
        "sys.stdout.buffer.write((os.path.basename(os.getcwd())+'|'+text).encode('utf-8'))",
    )

    output = provider.complete(system="système ✓", prompt="tâche 日本 🚀")

    assert "workspace café 日本 🚀" in output
    assert "système ✓" in output
    assert "tâche 日本 🚀" in output


def test_binary_stdin_is_utf8_even_when_child_locale_is_ascii(tmp_path: Path) -> None:
    environment = dict()
    # The child deliberately uses only binary stdio. An ASCII locale therefore cannot
    # corrupt the parent/child boundary.
    if sys.platform != "win32":
        environment.update({"PATH": str(Path(sys.executable).parent), "LANG": "C", "LC_ALL": "C"})
    else:
        environment.update({"PATH": str(Path(sys.executable).parent)})
    provider = CliProvider(
        [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.buffer.read(); data.decode('utf-8'); "
            "sys.stdout.buffer.write('résultat 日本'.encode('utf-8'))",
        ],
        cwd=tmp_path,
        provider_name="test-cli",
        environment=environment,
        timeout_seconds=5,
        heartbeat_interval_seconds=0.05,
        poll_interval_seconds=0.01,
    )

    assert provider.complete(system="règle", prompt="entrée 日本") == "résultat 日本"


def test_invalid_stdout_utf8_has_distinct_bounded_encoding_error(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        "import sys; sys.stdout.buffer.write(b'valid\\xffsecret-tail')",
    )

    with pytest.raises(ProviderEncodingError) as caught:
        provider.complete(system="system", prompt="task")

    assert caught.value.stream == "stdout"
    assert caught.value.offset == 5
    assert len(str(caught.value)) < 512
    assert "secret-tail" not in caught.value.preview


def test_invalid_stderr_utf8_is_distinguished_from_exit_failure(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        "import sys; sys.stderr.buffer.write(b'bad\\xff'); sys.stdout.write('ok')",
    )

    with pytest.raises(ProviderEncodingError) as caught:
        provider.complete(system="system", prompt="task")

    assert caught.value.stream == "stderr"


def test_large_stdout_is_fully_drained_then_fails_at_bounded_capture(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        "import sys; sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024)); sys.stdout.flush()",
        max_stdout_bytes=32 * 1024,
        io_chunk_bytes=4 * 1024,
    )

    with pytest.raises(ProviderOutputLimitError) as caught:
        provider.complete(system="system", prompt="task")

    assert caught.value.stream == "stdout"
    assert caught.value.limit_bytes == 32 * 1024
    assert caught.value.observed_bytes == 2 * 1024 * 1024


def test_large_stderr_cannot_deadlock_or_expand_error_without_bound(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        "import sys; sys.stderr.buffer.write(b'e' * (1024 * 1024)); sys.stderr.flush(); sys.exit(7)",
        max_stderr_bytes=16 * 1024,
        io_chunk_bytes=4 * 1024,
    )

    with pytest.raises(ProviderOutputLimitError) as caught:
        provider.complete(system="system", prompt="task")

    assert caught.value.stream == "stderr"
    assert caught.value.observed_bytes == 1024 * 1024
    assert len(str(caught.value)) < 256


def test_nonzero_exit_stderr_preview_is_bounded(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        "import sys; sys.stderr.write('failure-' + ('x' * 10000)); sys.exit(4)",
        max_stderr_bytes=32 * 1024,
    )

    with pytest.raises(ProviderExecutionError) as caught:
        provider.complete(system="system", prompt="task")

    text = str(caught.value)
    assert "exit code 4" in text
    assert "[truncated by SDAI]" in text
    assert len(text) < 5000


def test_missing_executable_has_distinct_startup_failure(tmp_path: Path) -> None:
    provider = CliProvider(
        ["sdai-command-that-does-not-exist-257"],
        cwd=tmp_path,
        provider_name="missing-cli",
    )

    with pytest.raises(ProviderStartupError) as caught:
        provider.complete(system="system", prompt="task")

    assert caught.value.reason_code == "executable-not-found"
    assert "sdai-command-that-does-not-exist-257" not in str(caught.value)


def test_timeout_remains_distinct_from_execution_and_encoding_failure(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        "import time; time.sleep(5)",
        timeout_seconds=1,
    )

    with pytest.raises(subprocess.TimeoutExpired):
        provider.complete(system="system", prompt="task")


def test_cancellation_still_terminates_managed_process_with_bounded_readers(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        "import sys,time; "
        "[(sys.stdout.write('tick\\n'),sys.stdout.flush(),time.sleep(0.02)) for _ in range(500)]",
        timeout_seconds=10,
    )
    token = ProviderCancellationToken()
    timer = Timer(0.15, token.cancel)
    timer.start()
    try:
        with pytest.raises(ProviderCancelledError):
            provider.complete_observable(
                system="system",
                prompt="task",
                cancellation=token,
                progress=lambda event: None,
            )
    finally:
        timer.cancel()


def test_command_arguments_are_not_shell_interpreted(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist.txt"
    payload = f"hello; echo compromised > {marker}"
    provider = CliProvider(
        [
            sys.executable,
            "-c",
            "import sys,json; print(json.dumps(sys.argv[1], ensure_ascii=False))",
            "{prompt}",
        ],
        cwd=tmp_path,
        provider_name="test-cli",
    )

    output = provider.complete(system="system", prompt=payload)

    decoded = json.loads(output)
    assert payload in decoded
    assert not marker.exists()


def test_io_limit_configuration_is_bounded(tmp_path: Path) -> None:
    provider = _provider(tmp_path, "print('ok')", max_stdout_bytes=0)
    with pytest.raises(ProviderExecutionError, match="max_stdout_bytes"):
        provider.complete(system="system", prompt="task")
