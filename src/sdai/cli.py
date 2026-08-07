from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sdai.agent_platform import AgentRuntime, Capability, ExecutionMode
from sdai.agent_platform.models import AgentInvocation
from sdai.agent_platform.profiles import load_profiles
from sdai.agent_platform.prompts import list_prompts, load_prompt
from sdai.agent_platform.skills import list_skills, load_skill
from sdai.agents.base import AgentResult
from sdai.artifacts import write_text
from sdai.models import LifecycleMode
from sdai.orchestrator import AGENTS, Orchestrator, StepExecution
from sdai.scaffold import init_project, upgrade_project
from sdai.validation import ValidationFinding, has_blockers, validate
from sdai.workflow_templates import install_v03_workflows
from sdai.workflows import grant_approval, load_workflow_state


def project_root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def ensure_initialized(root: Path) -> None:
    if not (root / ".sdai" / "config.yaml").exists():
        raise SystemExit("Not an SD-AI project. Run `sdai init` first.")


def cmd_init(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    try:
        created = init_project(root)
        created.extend(install_v03_workflows(root))
    except FileExistsError as exc:
        print(f"Already initialized: {exc}")
        return 1
    print(f"Initialized SD-AI project at {root}")
    for path in created:
        print(f"  + {path.relative_to(root)}")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    created = upgrade_project(root)
    created.extend(install_v03_workflows(root))
    if not created:
        print("SD-AI project already has the current agent and orchestration scaffold")
        return 0
    print(f"Upgraded SD-AI project at {root}")
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


def _print_findings(findings: list[ValidationFinding]) -> None:
    if not findings:
        print("  validation: passed")
        return
    for finding in findings:
        print(f"  {finding.level:5} {finding.code}: {finding.message}")


def _print_step_execution(execution: StepExecution, root: Path) -> None:
    print(f"[{execution.step_id}] type={execution.kind.value} status={execution.status}")
    if execution.message:
        print(f"  {execution.message}")
    result = execution.result
    if isinstance(result, AgentResult):
        _print_agent_result(result, root)
    elif isinstance(result, list) and all(isinstance(item, ValidationFinding) for item in result):
        _print_findings(result)
    elif isinstance(result, AgentInvocation):
        print(
            f"  profile={result.profile.name} provider={result.profile.provider} "
            f"capability={result.capability.value} mode={result.mode.value}"
        )
        print("\n--- SYSTEM ---\n")
        print(result.system)
        print("\n--- PROMPT ---\n")
        print(result.prompt)
    elif result is not None and hasattr(result, "profile") and hasattr(result, "provider"):
        print(f"  profile={result.profile} provider={result.provider}")


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
    _print_findings(findings)
    return 2 if has_blockers(findings) else 0


def cmd_run(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    results = Orchestrator(root).run_workflow(args.feature_id, args.workflow)
    exit_code = 0
    for execution in results:
        _print_step_execution(execution, root)
        if execution.status == "failed":
            exit_code = 2
        elif execution.status == "paused" and exit_code == 0:
            exit_code = 3
    return exit_code


def cmd_step(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    orchestrator = Orchestrator(root)
    definition = orchestrator.workflow_definition(args.workflow)
    context = orchestrator.context(args.feature_id)

    if args.step_action == "list":
        state = load_workflow_state(context, definition.name)
        for step in definition.steps:
            if state.is_complete(step.id):
                status = "completed"
            elif state.paused_at == step.id:
                status = "paused"
            else:
                status = "pending"
            detail = step.action or (step.capability.value if step.capability else step.gate or "")
            print(f"{step.id:24} type={step.kind.value:13} status={status:9} {detail}")
        return 0

    if args.step_action == "run":
        mode_override = ExecutionMode(args.mode) if args.mode else None
        execution = orchestrator.run_manual_step(
            args.feature_id,
            args.workflow,
            args.step_id,
            force=args.force,
            dry_run=args.dry_run,
            profile_override=args.profile,
            mode_override=mode_override,
        )
        _print_step_execution(execution, root)
        if execution.status == "failed":
            return 2
        if execution.status == "paused":
            return 3
        return 0

    raise ValueError(f"Unknown step action: {args.step_action}")


def cmd_approve(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    context = Orchestrator(root).context(args.feature_id)
    record = grant_approval(context, args.gate, approved_by=args.by, note=args.note or "")
    print(
        f"Approved gate '{record.gate}' for {args.feature_id} "
        f"by {record.approved_by} at {record.approved_at}"
    )
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    runtime = AgentRuntime(root)

    if args.agent_action == "list":
        for profile in load_profiles(root).values():
            capabilities = ",".join(value.value for value in profile.capabilities)
            state = "enabled" if profile.enabled else "disabled"
            print(f"{profile.name:14} provider={profile.provider:8} {state:8} capabilities={capabilities}")
        return 0

    if args.agent_action == "doctor":
        exit_code = 0
        for name, provider, available, detail in runtime.doctor():
            status = "OK" if available else "MISSING"
            print(f"{status:7} {name:14} provider={provider:8} {detail}")
            if not available and detail != "profile disabled":
                exit_code = 1
        return exit_code

    if args.agent_action == "run":
        capability = Capability(args.capability)
        mode = ExecutionMode(args.mode)
        if args.dry_run:
            invocation = runtime.build_invocation(
                args.feature_id,
                capability,
                profile_name=args.profile,
                mode=mode,
            )
            print(
                f"profile={invocation.profile.name} provider={invocation.profile.provider} "
                f"capability={capability.value} mode={mode.value}"
            )
            print("\n--- SYSTEM ---\n")
            print(invocation.system)
            print("\n--- PROMPT ---\n")
            print(invocation.prompt)
            return 0

        result = runtime.execute(
            args.feature_id,
            capability,
            profile_name=args.profile,
            mode=mode,
        )
        print(
            f"[{result.profile}/{result.provider}] capability={result.capability.value} "
            f"skills={','.join(result.skills) or '-'}"
        )
        print(result.output)
        return 0

    raise ValueError(f"Unknown agents action: {args.agent_action}")


def cmd_skills(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    if args.skill_action == "list":
        for skill in list_skills(root):
            capabilities = ",".join(value.value for value in skill.capabilities)
            print(f"{skill.name:20} capabilities={capabilities}\n  {skill.description}")
        return 0
    if args.skill_action == "show":
        skill = load_skill(root, args.name)
        print(f"# {skill.name}\n\n{skill.description}\n\n{skill.instructions}")
        return 0
    raise ValueError(f"Unknown skills action: {args.skill_action}")


def cmd_prompts(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    if args.prompt_action == "list":
        for name in list_prompts(root):
            print(name)
        return 0
    if args.prompt_action == "show":
        print(load_prompt(root, args.name))
        return 0
    raise ValueError(f"Unknown prompts action: {args.prompt_action}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sdai", description="Spec-Driven AI Development Framework")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize an SD-AI project")
    init.add_argument("--path")
    init.set_defaults(func=cmd_init)

    upgrade = sub.add_parser("upgrade", help="Add missing files for the current SD-AI scaffold without overwriting customizations")
    upgrade.add_argument("--path")
    upgrade.set_defaults(func=cmd_upgrade)

    feature = sub.add_parser("feature", help="Create a feature intake")
    feature.add_argument("feature_id")
    feature.add_argument("--title", required=True)
    feature.add_argument("--description", required=True)
    feature.add_argument("--workflow", default="standard", help="Workflow name; custom workflows are supported")
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

    run = sub.add_parser("run", help="Execute or resume a declarative lifecycle workflow")
    run.add_argument("feature_id")
    run.add_argument("--workflow", default="standard", help="Workflow file name under .sdai/workflows")
    run.add_argument("--path")
    run.set_defaults(func=cmd_run)

    step = sub.add_parser("step", help="List or manually run any workflow step at any time")
    step_sub = step.add_subparsers(dest="step_action", required=True)
    step_list = step_sub.add_parser("list", help="Show workflow steps and persisted execution status")
    step_list.add_argument("feature_id")
    step_list.add_argument("--workflow", default="standard")
    step_list.add_argument("--path")
    step_list.set_defaults(func=cmd_step)
    step_run = step_sub.add_parser("run", help="Run one named step independently of predecessor state")
    step_run.add_argument("feature_id")
    step_run.add_argument("step_id")
    step_run.add_argument("--workflow", default="standard")
    step_run.add_argument("--force", action="store_true", help="Rerun even when the step is already completed")
    step_run.add_argument("--dry-run", action="store_true", help="Render an external-agent step without invoking it")
    step_run.add_argument("--profile", help="Override the agent profile for this invocation")
    step_run.add_argument("--mode", choices=[m.value for m in ExecutionMode], help="Override the agent execution mode")
    step_run.add_argument("--path")
    step_run.set_defaults(func=cmd_step)

    approve = sub.add_parser("approve", help="Record a human approval gate for a feature")
    approve.add_argument("feature_id")
    approve.add_argument("gate")
    approve.add_argument("--by", required=True, help="Approver identity recorded in the approval artifact")
    approve.add_argument("--note")
    approve.add_argument("--path")
    approve.set_defaults(func=cmd_approve)

    agents = sub.add_parser("agents", help="Discover and execute external AI agent profiles")
    agents_sub = agents.add_subparsers(dest="agent_action", required=True)
    agents_list = agents_sub.add_parser("list")
    agents_list.add_argument("--path")
    agents_list.set_defaults(func=cmd_agents)
    agents_doctor = agents_sub.add_parser("doctor")
    agents_doctor.add_argument("--path")
    agents_doctor.set_defaults(func=cmd_agents)
    agents_run = agents_sub.add_parser("run")
    agents_run.add_argument("capability", choices=[c.value for c in Capability])
    agents_run.add_argument("feature_id")
    agents_run.add_argument("--profile", help="Override capability routing with a named agent profile")
    agents_run.add_argument("--mode", choices=[m.value for m in ExecutionMode], default="advisory")
    agents_run.add_argument("--dry-run", action="store_true", help="Render context, skills, and prompt without invoking an external agent")
    agents_run.add_argument("--path")
    agents_run.set_defaults(func=cmd_agents)

    skills = sub.add_parser("skills", help="List or inspect reusable SD-AI skills")
    skills_sub = skills.add_subparsers(dest="skill_action", required=True)
    skills_list = skills_sub.add_parser("list")
    skills_list.add_argument("--path")
    skills_list.set_defaults(func=cmd_skills)
    skills_show = skills_sub.add_parser("show")
    skills_show.add_argument("name")
    skills_show.add_argument("--path")
    skills_show.set_defaults(func=cmd_skills)

    prompts = sub.add_parser("prompts", help="List or inspect reusable SD-AI prompt templates")
    prompts_sub = prompts.add_subparsers(dest="prompt_action", required=True)
    prompts_list = prompts_sub.add_parser("list")
    prompts_list.add_argument("--path")
    prompts_list.set_defaults(func=cmd_prompts)
    prompts_show = prompts_sub.add_parser("show")
    prompts_show.add_argument("name")
    prompts_show.add_argument("--path")
    prompts_show.set_defaults(func=cmd_prompts)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
