from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from sdai.architecture_drift import ArchitectureDriftError
from sdai.architecture_engine import ARCHITECTURE_BLOCKED_EXIT_CODE, ArchitectureCheckResult, check_architecture


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdai architecture")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("inspect", "Inspect current repository architecture drift without failing on findings."),
        ("check", "Check current repository architecture drift and fail when effective policy blocks findings."),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument("feature", help="Feature/change id with approved architecture truth.")
        child.add_argument("--path", default=".", help="Project workspace path (default: current directory).")
        child.add_argument("--json", action="store_true", dest="json_output", help="Emit stable machine-readable JSON.")
    return parser


def _human(result: ArchitectureCheckResult) -> str:
    findings = result.report.findings
    lines = [
        f"Architecture {result.status}: {result.feature_id}",
        f"topology: {result.report.topology_sha256}",
        f"approval: {result.report.approval_truth_sha256}",
        f"report: {result.report.sha256}",
        f"policy: {result.policy.sha256}",
        f"findings: {len(findings)}",
        f"blocking: {len(result.blocking_codes)}",
    ]
    for finding in findings:
        marker = "BLOCK" if result.policy.blocks(finding) else "INFO"
        lines.append(
            f"[{marker}] {finding.code} {finding.kind.value} {finding.source} -> {finding.target}: {finding.message}"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = check_architecture(Path(args.path), args.feature)
    except (ArchitectureDriftError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json_output:
        sys.stdout.write(result.to_json())
    else:
        sys.stdout.write(_human(result))
    if args.command == "check" and result.blocked:
        return ARCHITECTURE_BLOCKED_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
