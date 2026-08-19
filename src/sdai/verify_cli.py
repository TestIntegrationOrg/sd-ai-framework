from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sdai.architecture_verify import verify_feature_with_architecture as verify_feature
from sdai.verification import VerificationFindingSource, VerificationOutcome


_RISKS = ("trivial", "standard", "critical", "regulated")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdai verify",
        description="Verify current feature truth without invoking an AI provider",
    )
    parser.add_argument("feature")
    parser.add_argument("--risk", choices=_RISKS, default="standard")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path")
    return parser


def _root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def _ensure_initialized(root: Path) -> None:
    if not (root / ".sdai" / "config.yaml").exists():
        raise RuntimeError("Not an SD-AI project. Run `sdai init` first.")


def _human(report) -> None:
    deterministic = sum(
        1 for item in report.findings if item.source is VerificationFindingSource.DETERMINISTIC
    )
    semantic = sum(
        1 for item in report.findings if item.source is VerificationFindingSource.SEMANTIC
    )
    print(
        f"Verify feature={report.feature_id} outcome={report.outcome.value} "
        f"deterministic={deterministic} semantic={semantic} "
        f"input={report.input_sha256} report={report.sha256}"
    )
    for source in (VerificationFindingSource.DETERMINISTIC, VerificationFindingSource.SEMANTIC):
        items = [item for item in report.findings if item.source is source]
        if not items:
            continue
        print(f"{source.value.capitalize()} findings:")
        for item in items:
            print(
                f"  {item.severity.value.upper():8} {item.status.value.upper():15} "
                f"{item.code}: {item.message}"
            )
            for provenance in item.provenance:
                print(f"    source={provenance.source}:{provenance.line}")
    if report.semantic_reviews:
        print("Semantic review evidence:")
        for review in report.semantic_reviews:
            print(
                f"  {review.dimension.value:24} subject={review.subject} "
                f"status={review.status.value} freshness={review.freshness.value} "
                f"current={str(review.satisfies_current_verification).lower()}"
            )


def main(argv: list[str] | None = None) -> int:
    effective = list(argv or [])
    if not effective or effective[0] in {"-h", "--help"}:
        print(_parser().format_help().rstrip())
        return 0
    try:
        args = _parser().parse_args(effective)
        root = _root(args.path)
        _ensure_initialized(root)
        report = verify_feature(root, args.feature, risk=args.risk)
        if args.json:
            print(report.to_json())
        else:
            _human(report)
        if report.outcome is VerificationOutcome.PASSED:
            return 0
        if report.outcome is VerificationOutcome.REVIEW:
            return 3
        return 2
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
