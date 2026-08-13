from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from sdai.analysis_rules import analyze_feature


_SEVERITY_ORDER = {
    "blocking": 0,
    "warning": 1,
    "suggestion": 2,
    "info": 3,
}


def add_analysis_parser(commands: argparse._SubParsersAction) -> None:
    analyze = commands.add_parser(
        "analyze",
        help="Run deterministic read-only cross-artifact consistency analysis",
    )
    analyze.add_argument("feature")
    analyze.add_argument(
        "--risk",
        choices=["trivial", "standard", "critical", "regulated"],
        default="standard",
        help="Artifact-state risk profile used for stale-evidence analysis",
    )
    analyze.add_argument("--json", action="store_true")
    analyze.add_argument("--path")


def _human_output(report) -> None:
    counts = Counter(item.severity for item in report.findings)
    print(
        f"Analysis feature={report.feature_id} findings={len(report.findings)} "
        f"blocking={counts['blocking']} warnings={counts['warning']} "
        f"suggestions={counts['suggestion']} info={counts['info']}"
    )
    print(f"Index: {report.index_sha256}")
    ordered = sorted(
        report.findings,
        key=lambda item: (
            _SEVERITY_ORDER[item.severity],
            item.code,
            item.entity_id or "",
            item.message,
        ),
    )
    for finding in ordered:
        entity = f" entity={finding.entity_id}" if finding.entity_id else ""
        print(f"{finding.severity.upper():10} {finding.code}{entity}: {finding.message}")
        for evidence in finding.evidence:
            detail = f" — {evidence.detail}" if evidence.detail else ""
            evidence_entity = f" [{evidence.entity_id}]" if evidence.entity_id else ""
            print(f"  {evidence.source}:{evidence.line}{evidence_entity}{detail}")


def run_analysis_command(root: Path, args: argparse.Namespace) -> int:
    report = analyze_feature(
        root,
        args.feature,
        risk=args.risk,
    )
    if args.json:
        print(report.to_json())
    else:
        _human_output(report)
    return 2 if any(item.severity == "blocking" for item in report.findings) else 0
