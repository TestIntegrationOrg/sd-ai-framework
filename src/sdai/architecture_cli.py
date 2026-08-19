from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sdai.architecture_engine import ArchitectureDriftEvaluation, evaluate_architecture_drift


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdai architecture drift",
        description="Evaluate approved architecture against deterministic repository reality",
    )
    parser.add_argument("feature")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path")
    return parser


def _human(evaluation: ArchitectureDriftEvaluation) -> None:
    report = evaluation.report
    finding_count = len(report.findings) if report is not None else 0
    print(
        f"Architecture drift feature={evaluation.feature_id} "
        f"outcome={evaluation.decision.outcome} topology={str(evaluation.topology_present).lower()} "
        f"findings={finding_count} policy={evaluation.policy.sha256} "
        f"evaluation={evaluation.sha256}"
    )
    if evaluation.governance_error:
        print(f"  BLOCKED {evaluation.governance_error}")
    if report is not None:
        for finding in report.findings:
            threshold = evaluation.policy.threshold_for(finding.kind)
            blocked = evaluation.policy.blocks(finding)
            print(
                f"  {finding.severity.value.upper():7} {finding.kind.value:16} {finding.code} "
                f"blocked={str(blocked).lower()} threshold={threshold.value}: {finding.message}"
            )
            for provenance in (*finding.approved_provenance, *finding.observed_provenance):
                print(f"    source={provenance.source}:{provenance.line}")
    for blocker in evaluation.decision.blockers:
        if report is None or blocker.code.startswith("ARCH-POLICY-"):
            print(f"  POLICY {blocker.code}: {blocker.reason}")


def main(argv: list[str] | None = None) -> int:
    effective = list(argv or [])
    if not effective or effective[0] in {"-h", "--help"}:
        print(_parser().format_help().rstrip())
        return 0
    try:
        args = _parser().parse_args(effective)
        evaluation = evaluate_architecture_drift(
            Path(args.path or ".").resolve(),
            args.feature,
        )
        if args.json:
            sys.stdout.write(evaluation.to_json())
        else:
            _human(evaluation)
        return 2 if evaluation.blocked else 0
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
