from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys

from sdai import __version__
from sdai.converge_cli import main as converge_main
from sdai.entrypoint import main as lifecycle_main
from sdai.trace_cli import main as trace_main
from sdai.trace_policy_cli import main as trace_policy_main
from sdai.verify_cli import main as verify_main
from sdai.versioning import write_framework_metadata


def _project_root(argv: list[str]) -> Path:
    for index, value in enumerate(argv):
        if value == "--path" and index + 1 < len(argv):
            return Path(argv[index + 1]).resolve()
        if value.startswith("--path="):
            return Path(value.split("=", 1)[1]).resolve()
    return Path(".").resolve()


def _run_trace(argv: list[str]) -> int:
    if argv and argv[0] == "policy":
        return trace_policy_main(argv[1:])

    # `TraceGraph.to_json()` already includes its canonical trailing newline.
    # The trace module currently uses print() for export, so normalize only the
    # versioned executable boundary to avoid adding a second newline. This keeps
    # `sdai trace export ...` byte-for-byte equal to the canonical graph JSON.
    if argv and argv[0] == "export":
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = trace_main(argv)
        output = buffer.getvalue()
        if output.endswith("\n\n"):
            output = output[:-1]
        sys.stdout.write(output)
        return result
    return trace_main(argv)


def main(argv: list[str] | None = None) -> int:
    effective = list(sys.argv[1:] if argv is None else argv)
    if effective in (["--version"], ["-V"]):
        print(f"sdai {__version__}")
        return 0

    if effective and effective[0] == "trace":
        return _run_trace(effective[1:])

    if effective and effective[0] == "verify":
        return verify_main(effective[1:])

    if effective and effective[0] == "converge":
        return converge_main(effective[1:])

    result = lifecycle_main(effective)
    if result == 0 and effective and effective[0] in {"init", "upgrade"}:
        root = _project_root(effective)
        metadata = write_framework_metadata(root)
        print(f"SD-AI framework version {__version__}")
        print(f"  + {metadata.relative_to(root).as_posix()}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())