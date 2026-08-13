from __future__ import annotations

from pathlib import Path
import sys

from sdai import __version__
from sdai.entrypoint import main as lifecycle_main
from sdai.trace_cli import main as trace_main
from sdai.versioning import write_framework_metadata


def _project_root(argv: list[str]) -> Path:
    for index, value in enumerate(argv):
        if value == "--path" and index + 1 < len(argv):
            return Path(argv[index + 1]).resolve()
        if value.startswith("--path="):
            return Path(value.split("=", 1)[1]).resolve()
    return Path(".").resolve()


def main(argv: list[str] | None = None) -> int:
    effective = list(sys.argv[1:] if argv is None else argv)
    if effective in (["--version"], ["-V"]):
        print(f"sdai {__version__}")
        return 0

    if effective and effective[0] == "trace":
        return trace_main(effective[1:])

    result = lifecycle_main(effective)
    if result == 0 and effective and effective[0] in {"init", "upgrade"}:
        root = _project_root(effective)
        metadata = write_framework_metadata(root)
        print(f"SD-AI framework version {__version__}")
        print(f"  + {metadata.relative_to(root).as_posix()}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
