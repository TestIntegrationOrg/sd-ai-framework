from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sdai.convergence import ConvergenceStatus, run_convergence


_RISKS = ("trivial", "standard", "critical", "regulated")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdai converge",
        description="Create bounded deterministic remediation work from current verification gaps",
    )
    parser.add_argument("feature")
    parser.add_argument("--risk", choices=_RISKS, default="standard")
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path")
    return parser


def _root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def _ensure_initialized(root: Path) -> None:
    if not (root / ".sdai" / "config.yaml").exists():
        raise RuntimeError("Not an SD-AI project. Run `sdai init` first.")


def _human(state) -> None:
    escalation = (
        "-" if state.escalation_reason is None else state.escalation_reason.value
    )
    print(
        f"Converge feature={state.feature_id} status={state.status.value} "
        f"risk={state.risk} rounds={len(state.rounds)}/{state.max_rounds} "
        f"escalation={escalation} verification={state.current_verification_report_sha256}"
    )
    for round_state in state.rounds:
        print(
            f"  ROUND {round_state.number} id={round_state.round_id} "
            f"outcome={round_state.verification_outcome.value} "
            f"tasks={len(round_state.task_ids)} non_remediable={len(round_state.non_remediable)}"
        )
        for item in round_state.non_remediable:
            print(f"    ESCALATE {item}")
    if state.tasks:
        print("Remediation tasks:")
        for task in state.tasks:
            print(
                f"  {task.task_id} kind={task.remediation_kind.value} "
                f"finding={task.finding_code} subject={task.subject or '-'}"
            )
            print("    allowed=" + ",".join(task.allowed_roots))
            print("    forbidden=" + ",".join(task.forbidden_roots))


def main(argv: list[str] | None = None) -> int:
    effective = list(argv or [])
    if not effective or effective[0] in {"-h", "--help"}:
        print(_parser().format_help().rstrip())
        return 0
    try:
        args = _parser().parse_args(effective)
        root = _root(args.path)
        _ensure_initialized(root)
        state = run_convergence(
            root,
            args.feature,
            risk=args.risk,
            max_rounds=args.max_rounds,
        )
        if args.json:
            print(state.to_json())
        else:
            _human(state)
        if state.status is ConvergenceStatus.VERIFIED:
            return 0
        if state.status is ConvergenceStatus.ACTION_REQUIRED:
            return 3
        return 2
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
