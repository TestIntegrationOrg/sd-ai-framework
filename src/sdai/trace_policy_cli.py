from __future__ import annotations

import argparse
from pathlib import Path

from sdai.trace_policy import CoverageDimension, evaluate_trace_policy


RISKS = ("trivial", "standard", "critical", "regulated")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdai trace policy")
    parser.add_argument("feature")
    parser.add_argument("--risk", choices=RISKS, default="standard")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path")
    return parser


def _root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def _ensure_initialized(root: Path) -> None:
    if not (root / ".sdai" / "config.yaml").exists():
        raise RuntimeError("Not an SD-AI project. Run `sdai init` first.")


def _human(report) -> None:
    print(
        f"Trace policy feature={report.feature_id} risk={report.risk} "
        f"passed={str(report.passed).lower()} sha256={report.graph_sha256}"
    )
    for item in report.dimensions:
        status = "PASS" if item.compliant else "BLOCK"
        print(
            f"  {status:5} {item.dimension.value:12} "
            f"actual={item.actual_percent:.2f}% required={item.threshold.required_percent:.2f}% "
            f"count={item.numerator}/{item.denominator}"
        )
        for contribution in item.threshold.contributions:
            marker = "*" if contribution.value == item.threshold.required_percent else " "
            print(
                f"    {marker} {contribution.layer.value:7} value={contribution.value:.2f}% "
                f"policy={contribution.policy_id} source={contribution.source}"
            )
    for finding in report.findings:
        print(f"  {finding.severity.upper():8} {finding.code}: {finding.message}")


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(list(argv or []))
        root = _root(args.path)
        _ensure_initialized(root)
        report = evaluate_trace_policy(root, args.feature, args.risk)
        if args.json:
            print(report.to_json())
        else:
            _human(report)
        return 0 if report.passed else 2
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 1


__all__ = ["CoverageDimension", "main"]
