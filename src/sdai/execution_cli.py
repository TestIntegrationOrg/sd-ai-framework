from __future__ import annotations

import argparse
from pathlib import Path

from sdai.execution_resume import build_resume_plan, resume_execution


def add_execution_parser(commands: argparse._SubParsersAction) -> None:
    execution = commands.add_parser(
        "execution",
        help="Inspect or resume durable implementation execution state",
    )
    actions = execution.add_subparsers(dest="execution_action", required=True)

    status = actions.add_parser(
        "status",
        help="Verify durable task state and show the exact resume point",
    )
    status.add_argument("feature")
    status.add_argument("--run", required=True, dest="run_id")
    status.add_argument("--json", action="store_true")
    status.add_argument("--path")

    resume = actions.add_parser(
        "resume",
        help="Reserve the exact first incomplete task using durable evidence",
    )
    resume.add_argument("feature")
    resume.add_argument("--run", required=True, dest="run_id")
    resume.add_argument("--json", action="store_true")
    resume.add_argument("--path")


def _print_plan_human(plan) -> None:
    print(
        f"Execution status feature={plan.feature_id} run={plan.run_id} "
        f"run_status={plan.run_status} head={plan.current_head} "
        f"checkpoint={plan.checkpoint_status} clean={str(plan.repository_clean).lower()}"
    )
    for task in plan.tasks:
        marker = "SKIP" if task.skip_verified else task.action.upper()
        reason = ",".join(task.reasons) if task.reasons else "verified"
        dispatch = f" dispatch={task.existing_dispatch_id}" if task.existing_dispatch_id else ""
        print(
            f"  {marker:8} {task.task_id} attempt={task.attempt} "
            f"status={task.current_status} reason={reason}{dispatch}"
        )
    if plan.blocked_reason:
        print(f"Resume blocked: {plan.blocked_reason}")
    elif plan.resume_task_id:
        print(
            f"Resume point: {plan.resume_task_id} action={plan.resume_action} "
            f"plan={plan.plan_sha256}"
        )
    else:
        print("Resume point: none; all registered tasks have verified completion evidence")


def run_execution_command(root: Path, args: argparse.Namespace) -> int:
    if args.execution_action == "status":
        plan = build_resume_plan(root, args.feature, args.run_id)
        if args.json:
            print(plan.to_json(), end="")
        else:
            _print_plan_human(plan)
        return 0

    if args.execution_action == "resume":
        result = resume_execution(root, args.feature, args.run_id)
        if args.json:
            print(result.to_json(), end="")
        else:
            _print_plan_human(result.plan)
            if result.status == "ready":
                mode = "reused" if result.dispatch_reused else "reserved"
                print(f"Dispatch {mode}: {result.dispatch_id}")
                print(f"Checkpoint: {result.checkpoint_path}")
            elif result.status == "nothing-to-resume":
                print(f"Nothing to resume. Checkpoint: {result.checkpoint_path}")
        return 2 if result.status == "blocked" else 0

    raise ValueError(f"Unknown execution action: {args.execution_action}")
