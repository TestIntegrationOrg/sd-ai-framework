from __future__ import annotations

import argparse
from pathlib import Path

from sdai.artifact_state import ArtifactFreshness, evaluate_artifact_states


def add_artifact_state_parser(commands: argparse._SubParsersAction) -> None:
    artifact = commands.add_parser(
        "artifact",
        help="Inspect hash-bound artifact freshness and evidence state",
    )
    actions = artifact.add_subparsers(dest="artifact_action", required=True)

    status = actions.add_parser(
        "status",
        help="Evaluate fresh/stale/missing/blocked state for an artifact DAG",
    )
    status.add_argument("feature")
    status.add_argument("--risk", default="standard")
    status.add_argument("--domain")
    status.add_argument("--json", action="store_true")
    status.add_argument("--path")

    explain = actions.add_parser(
        "explain",
        help="Explain freshness and evidence for one artifact",
    )
    explain.add_argument("feature")
    explain.add_argument("artifact_id")
    explain.add_argument("--risk", default="standard")
    explain.add_argument("--domain")
    explain.add_argument("--json", action="store_true")
    explain.add_argument("--path")


def _print_state(item) -> None:
    marker = {
        ArtifactFreshness.FRESH: "FRESH",
        ArtifactFreshness.STALE: "STALE",
        ArtifactFreshness.MISSING: "MISSING",
        ArtifactFreshness.BLOCKED: "BLOCKED",
    }[item.freshness]
    print(
        f"  {marker:7} {item.artifact_id} path={item.path} "
        f"required={str(item.required).lower()}"
    )
    for reason in item.reasons:
        print(f"           reason: {reason}")
    for evidence in item.evidence:
        state = "fresh" if evidence.fresh else "stale"
        print(
            f"           evidence {evidence.kind}/{evidence.id} "
            f"state={state} source={evidence.source}"
        )


def run_artifact_state_command(root: Path, args: argparse.Namespace) -> int:
    report = evaluate_artifact_states(
        root,
        args.feature,
        risk=args.risk,
        domain=args.domain,
    )
    if args.artifact_action == "status":
        if args.json:
            print(report.to_json())
        else:
            counts = report.as_dict()["counts"]
            print(
                f"Artifact state feature={report.feature_id} risk={report.risk} "
                f"fresh={counts['fresh']} stale={counts['stale']} "
                f"missing={counts['missing']} blocked={counts['blocked']}"
            )
            for item in report.states:
                _print_state(item)
        return 0

    if args.artifact_action == "explain":
        item = report.by_id().get(args.artifact_id)
        if item is None:
            raise ValueError(
                f"artifact '{args.artifact_id}' is not active for risk profile '{report.risk}'"
            )
        if args.json:
            import json

            print(json.dumps(item.as_dict(), indent=2, sort_keys=True, ensure_ascii=False))
        else:
            _print_state(item)
        return 0

    raise ValueError(f"Unknown artifact action: {args.artifact_action}")
