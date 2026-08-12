from __future__ import annotations

import argparse
from pathlib import Path

from sdai.skill_resolution import resolve_skills


def add_skill_resolution_parser(actions: argparse._SubParsersAction) -> None:
    resolve = actions.add_parser(
        "resolve",
        help="Resolve and explain the minimal compatible skill set for a semantic role",
    )
    resolve.add_argument("--agent", required=True, help="Semantic agent name")
    resolve.add_argument("--capability", required=True)
    resolve.add_argument("--task")
    resolve.add_argument("--domain")
    resolve.add_argument(
        "--skill",
        dest="skills",
        action="append",
        default=[],
        help="Explicitly require an additional skill; repeatable",
    )
    resolve.add_argument("--json", action="store_true")
    resolve.add_argument("--path")


def run_skill_resolution_command(root: Path, args: argparse.Namespace) -> int:
    report = resolve_skills(
        root,
        agent_name=args.agent,
        capability=args.capability,
        task=args.task,
        domain=args.domain,
        requested=tuple(args.skills),
    )
    if args.json:
        print(report.to_json())
        return 0

    print(
        f"Skill resolution agent={report.agent} capability={report.capability} "
        f"selected={len(report.selected)} policy_required={len(report.policy_required)}"
    )
    if report.task:
        print(f"  task: {report.task}")
    if report.domain:
        print(f"  domain: {report.domain}")
    for name in report.selected:
        decision = next(item for item in report.decisions if item.name == name)
        origins = ",".join(decision.origins) or "-"
        print(f"  SELECT {name} origins={origins}")
    for decision in report.decisions:
        if decision.selected:
            continue
        print(f"  SKIP   {decision.name}: {'; '.join(decision.reasons)}")
    return 0
