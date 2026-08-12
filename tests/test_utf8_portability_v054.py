from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from sdai.artifacts import read_text, write_text
from sdai.providers.cli import CliProvider, ProviderExecutionError
from sdai.text import TextEncodingError, read_utf8_text


UNICODE_TEXT = "café 東京 😀 – user’s request → ✓"


def _python_provider(tmp_path: Path, source: str, name: str = "test-provider") -> CliProvider:
    return CliProvider(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        provider_name=name,
    )


def test_provider_stdin_and_stdout_are_strict_utf8_bytes(tmp_path: Path):
    provider = _python_provider(
        tmp_path,
        "import sys; data=sys.stdin.buffer.read(); "
        "text=data.decode('utf-8', errors='strict'); "
        "sys.stdout.buffer.write(text.encode('utf-8'))",
    )

    output = provider.complete(system="UTF-8 system", prompt=UNICODE_TEXT)

    assert UNICODE_TEXT in output


def test_provider_output_strips_only_leading_utf8_bom_before_json(tmp_path: Path):
    provider = _python_provider(
        tmp_path,
        "import sys; "
        "sys.stdout.buffer.write(b'\\xef\\xbb\\xbf  {\"value\": \"caf\\xc3\\xa9\"}  ')",
    )

    output = provider.complete(system="system", prompt="task")

    assert json.loads(output) == {"value": "café"}
    assert not output.startswith("\ufeff")


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_provider_invalid_utf8_fails_with_identity_and_escaped_preview(
    tmp_path: Path, stream: str
):
    target = "stdout" if stream == "stdout" else "stderr"
    provider = _python_provider(
        tmp_path,
        f"import sys; sys.{target}.buffer.write(b'bad-\\x96-byte')",
        name="codex-test",
    )

    with pytest.raises(ProviderExecutionError) as error:
        provider.complete(system="system", prompt="task")

    message = str(error.value)
    assert "codex-test" in message
    assert f"on {stream}" in message
    assert "\\x96" in message
    assert "invalid UTF-8" in message


def test_repository_reader_accepts_utf8_bom(tmp_path: Path):
    path = tmp_path / "specification.md"
    path.write_bytes(b"\xef\xbb\xbf" + UNICODE_TEXT.encode("utf-8"))

    assert read_text(path) == UNICODE_TEXT
    assert read_utf8_text(path) == UNICODE_TEXT


def test_repository_reader_rejects_cp1252_with_actionable_diagnostic(tmp_path: Path):
    path = tmp_path / "specification.md"
    path.write_bytes(b"Retry payment \x96 maximum 3 attempts")

    with pytest.raises(TextEncodingError) as error:
        read_text(path)

    message = str(error.value)
    assert str(path) in message
    assert "not valid UTF-8" in message
    assert "\\x96" in message
    assert "Convert the file to UTF-8" in message


def test_repository_writer_uses_utf8_without_bom_and_lf(tmp_path: Path):
    path = tmp_path / "artifact.md"

    write_text(path, f"{UNICODE_TEXT}\r\nsecond line")

    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in data
    assert data == f"{UNICODE_TEXT}\nsecond line\n".encode("utf-8")
