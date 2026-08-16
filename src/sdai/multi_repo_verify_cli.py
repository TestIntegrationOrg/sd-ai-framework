from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sdai.multi_repo_run import MultiRepoRunError
from sdai.multi_repo_verify import verify_all_repositories


_RISKS = ("trivial", "standard", "critical", "regulated")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdai verify --all-repos",
        description="Aggregate feature verification across explicit repository participants",
    )
    parser.add_argument("--all-repos", action="store_true", required=True)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--risk", choices=_RISKS, default="standard")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path", help="Central SD-AI project root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.path or ".").resolve()
    try:
        report = verify_all_repositories(
            root,
            args.feature,
            risk=args.risk,
        )
    except (MultiRepoRunError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 6
    if args.json:
        print(report.to_json())
    else:
        print(
            f"All-repo verification feature={report.feature_id} "
            f"exit-class={report.exit_class.name.lower().replace('_', '-')} "
            f"repositories={len(report.repositories)} graph={report.graph_sha256}"
        )
        if report.graph_findings:
            print(f"  graph-findings={len(report.graph_findings)}")
        for item in report.repositories:
            print(
                f"  {item.repository_id:20} status={item.status:22} exit={item.exit_code}"
            )
            if item.reason:
                print(f"    {item.reason}")
    return int(report.exit_class)


if __name__ == "__main__":
    raise SystemExit(main())
