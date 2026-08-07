from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sdai.agents.base import AgentResult
from sdai.artifacts import write_text
from sdai.models import LifecycleMode
from sdai.orchestrator import AGENTS, Orchestrator
from sdai.scaffold import init_project
from sdai.validation import has_blockers, validate


def project_root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def ensure_initialized(root: Path) -> None:
    if not (root / ".sdai" / "config.yaml").exists():
        raise SystemExit("Not an SD-AI project. Run `sdai init` first.")


def cmd_init(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    try:
        created = init_project(root)
    except FileExistsError as exc:
        print(f"Already initialized: {exc}")
        return 1
    print(f"Initialized SD-AI project at {root}")
    for path in created:
        print(f"  + {path.relative_to(root)}")
    return 0


def cmd_feature(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    feature_dir = root / "specs" / args.feature_id
    intake = f"""# Feature Intake — {args.feature_id}

## Title
{args.title}

## Description
{args.description}

## Requested Lifecycle
{args.workflow}

## Source
manual

## Status
intake
"""
    write_text(feature_dir / "00-intake.md", intake, overwrite=False)
    print(f"Created specs/{args.feature_id}/00-intake.md")
    return 0


def _print_agent_result(result: AgentResult, root: Path) -> None:
    print(f"[{result.agent}] {result.summary}")
    for artifact in result.artifacts:
        print(f"  + {artifact.relative_to(root)}")


def cmd_agent(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    orchestrator = Orchestrator(root)
    result = AGENTS[args.command].run(orchestrator.context(args.feature_id))
    _print_agent_result(result, root)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    mode = LifecycleMode(args.workflow)
    findings = validate(Orchestrator(root).context(args.feature_id), mode)
    if not findings:
        print(f"Validation passed for {args.feature_id} ({mode.value})")
        return 0
    for finding in findings:
        print(f"{finding.level:5} {finding.code}: {finding.message}")
    return 2 if has_blockers(findings) else 0


def cmd_run(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    mode = LifecycleMode(args.workflow)
    results = Orchestrator(root).run_workflow(args.feature_id, mode)
    exit_code = 0
    for step, result in results:
        if isinstance(result, AgentResult):
            _print_agent_result(result, root)
        else:
            for finding in result:
                print(f"{finding.level:5} {finding.code}: {finding.message}")
            if has_blockers(result):
                exit_code = 2
    return exit_code


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sdai", description="Spec-Driven AI Development Framework")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize an SD-AI project")
    init.add_argument("--path")
    init.set_defaults(func=cmd_init)

    feature = sub.add_parser("feature", help="Create a feature intake")
    feature.add_argument("feature_id")
    feature.add_argument("--title", required=True)
    feature.add_argument("--description", required=True)
    feature.add_argument("--workflow", choices=[m.value for m in LifecycleMode], default="standard")
    feature.add_argument("--path")
    feature.set_defaults(func=cmd_feature)

    for name in ("specify", "architect", "plan", "implement", "security"):
        command = sub.add_parser(name)
        command.add_argument("feature_id")
        command.add_argument("--path")
        command.set_defaults(func=cmd_agent)

    validation = sub.add_parser("validate")
    validation.add_argument("feature_id")
    validation.add_argument("--workflow", choices=[m.value for m in LifecycleMode], default="standard")
    validation.add_argument("--path")
    validation.set_defaults(func=cmd_validate)

    run = sub.add_parser("run", help="Execute a declarative lifecycle workflow")
    run.add_argument("feature_id")
    run.add_argument("--workflow", choices=[m.value for m in LifecycleMode], default="standard")
    run.add_argument("--path")
    run.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
