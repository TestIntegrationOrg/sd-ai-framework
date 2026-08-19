from __future__ import annotations

import argparse
import json
from pathlib import Path

from sdai.technology import CATEGORIES, TechnologyReport, detect_technologies


TECHNOLOGY_REPORT_API_VERSION = "sdai.technology-report/v1"


def add_tech_parser(commands: argparse._SubParsersAction) -> None:
    tech = commands.add_parser(
        "tech",
        help="Detect repository languages, frameworks, build tools, platforms, libraries, and testing technologies",
    )
    actions = tech.add_subparsers(dest="tech_action", required=True)
    detect = actions.add_parser("detect", help="Detect repository technologies deterministically")
    detect.add_argument("--json", action="store_true")
    detect.add_argument("--path")


def _print_report(report: TechnologyReport) -> None:
    print(
        f"Technology detection technologies={len(report.technologies)} "
        f"findings={len(report.findings)} config={report.config_source or '-'}"
    )
    grouped = report.by_category()
    for category in CATEGORIES:
        if not grouped[category]:
            continue
        print(f"  {category}:")
        for item in grouped[category]:
            version = item.version or "-"
            print(
                f"    {item.name} version={version} source={item.version_source} "
                f"declared={str(item.declared).lower()} evidence={len(item.evidence)}"
            )
            for evidence in item.evidence:
                detail = f" detail={evidence.detail}" if evidence.detail else ""
                evidence_version = f" version={evidence.version}" if evidence.version else ""
                print(
                    f"      {evidence.source} detector={evidence.detector}"
                    f"{evidence_version}{detail}"
                )
    for finding in report.findings:
        source = f" source={finding.source}" if finding.source else ""
        print(
            f"  {finding.severity.upper():7} {finding.code}{source}: {finding.message}"
        )


def run_tech_command(root: Path, args: argparse.Namespace) -> int:
    if args.tech_action != "detect":
        raise ValueError(f"Unknown tech action: {args.tech_action}")
    report = detect_technologies(root)
    if args.json:
        payload = {"apiVersion": TECHNOLOGY_REPORT_API_VERSION, **report.as_dict()}
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        _print_report(report)
    return 0
