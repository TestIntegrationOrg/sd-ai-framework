from __future__ import annotations

from pathlib import Path


class TextEncodingError(RuntimeError):
    pass


def _escaped_byte_preview(data: bytes, start: int, end: int) -> str:
    return "".join(f"\\x{value:02x}" for value in data[start:end])


def read_utf8_text(path: Path) -> str:
    """Read UTF-8 repository text, accepting an optional leading UTF-8 BOM."""
    data = path.read_bytes()
    try:
        decoded = data.decode("utf-8-sig", errors="strict")
        return decoded.replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        preview = _escaped_byte_preview(data, exc.start, exc.end)
        raise TextEncodingError(
            f"{path} is not valid UTF-8 at byte {exc.start}; "
            f"offending-byte preview: {preview}. Convert the file to UTF-8 and retry."
        ) from exc


def write_utf8_text(path: Path, content: str) -> int:
    """Write portable UTF-8 without a BOM and with LF line endings."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return path.write_text(normalized, encoding="utf-8", errors="strict", newline="\n")
