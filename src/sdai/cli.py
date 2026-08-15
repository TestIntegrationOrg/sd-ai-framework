from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from sdai.agent_platform import AgentRuntime, Capability, ExecutionMode
from sdai.agent_platform.definitions import list_agent_definitions, load_agent_definition
from sdai.agent_platform.models import AgentInvocation
from sdai.agent_platform.native import PROVIDERS, sync_native_agents
from sdai.agent_platform.profiles import load_profiles
from sdai.agent_platform.prompts import list_prompts, load_prompt
from sdai.agent_platform.skills import list_skills, load_skill, validate_skills
from sdai.agents.base import AgentResult
from sdai.artifacts import write_text
from sdai.config import load_yaml
from sdai.enterprise_scaffold import install_v04_scaffold
from sdai.governance import check_workflow_governance
from sdai.integrations.github import (
    GitHubCli,
    PullRequestRequest,
    build_pull_request_body,
    github_issue_intake,
)
from sdai.integrations.jira import JiraClient, jira_issue_intake
from sdai.models import FeatureContext, LifecycleMode, validate_feature_id
from sdai.orchestrator import AGENTS, Orchestrator, StepExecution
from sdai.quality_gates import QualityGateResult, QualityGateRunner, load_quality_gates
from sdai.scaffold import init_project, upgrade_project
from sdai.specification_store_cli import add_store_parser
from sdai.text import read_utf8_text
from sdai.v05_scaffold import install_v05_scaffold
from sdai.validation import ValidationFinding, has_blockers, validate
from sdai.workflow_templates import install_current_workflows
from sdai.workflows import grant_approval, load_workflow_state
from sdai.worktree_isolation import create_worktree_session


def project_root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def ensure_initialized(root: Path) -> None:
    if not (root / ".sdai" / "config.yaml").exists():
        raise SystemExit("Not an SD-AI project. Run `sdai init` first.")


def _install_current(root: Path) -> list[Path]:
    created = install_v04_scaffold(root)
    created.extend(install_v05_scaffold(root))
    created.extend(install_current_workflows(root))
    return created


def cmd_init(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    try:
        created = init_project(root)
        created.extend(_install_current(root))
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
    created.extend(_install_current(root))
    if not created:
        print("SD-AI project already has the current scaffold")
        return 0
    print(f"Upgraded SD-AI project at {root}")
    for path in created:
        print(f"  + {path.relative_to(root)}")
    return 0


def cmd_feature(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    feature_id = validate_feature_id(args.feature_id)
    context = FeatureContext(root, feature_id)
    intake = f"""# Feature Intake — {feature_id}

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
    write_text(context.artifact("00-intake.md"), intake, overwrite=False)
    print(f"Created specs/{feature_id}/00-intake.md")
    return 0


def _print_agent_result(result: AgentResult, root: Path, *, indent: str = "") -> None:
    print(f"{indent}[{result.agent}] {result.summary}")
    for artifact in result.artifacts:
        print(f"{indent}  + {artifact.relative_to(root)}")


def _print_findings(findings: list[ValidationFinding], *, indent: str = "") -> None:
    if not findings:
        print(f"{indent}validation: passed")
        return
    for finding in findings:
        print(f"{indent}{finding.level:5} {finding.code}: {finding.message}")


def _print_step_execution(execution: StepExecution, root: Path, *, indent: str = "") -> None:
    attempts = f" attempts={execution.attempts}" if execution.attempts > 1 else ""
    print(f"{indent}[{execution.step_id}] type={execution.kind.value} status={execution.status}{attempts}")
    if execution.message:
        print(f"{indent}  {execution.message}")
    result = execution.result
    if isinstance(result, AgentResult):
        _print_agent_result(result, root, indent=indent + "  ")
    elif isinstance(result, list) and all(isinstance(item, ValidationFinding) for item in result):
        _print_findings(result, indent=indent + "  ")
    elif isinstance(result, list) and all(isinstance(item, StepExecution) for item in result):
        for child in result:
            _print_step_execution(child, root, indent=indent + "  ")
    elif isinstance(result, AgentInvocation):
        semantic = result.agent_name or "-"
        print(
            f"{indent}  agent={semantic} profile={result.profile.name} "
            f"provider={result.profile.provider} capability={result.capability.value} mode={result.mode.value}"
        )
        print(f"{indent}\n--- SYSTEM ---\n")
        print(result.system)
        print(f"{indent}\n--- PROMPT ---\n")
        print(result.prompt)
    elif isinstance(result, QualityGateResult):
        print(f"{indent}  exit_code={result.return_code} passed={result.passed}")
        if result.artifact:
            print(f"{indent}  + {result.artifact.relative_to(root)}")
    elif result is not None and hasattr(result, "profile") and hasattr(result, "provider"):
        semantic = getattr(result, "agent_name", None) or "-"
        print(f"{indent}  agent={semantic} profile={result.profile} provider={result.provider}")


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


def _workflow_exit_code(results: list[StepExecution]) -> int:
    exit_code = 0
    for execution in results:
        if execution.status == "failed":
            exit_code = 2
        elif execution.status == "paused" and exit_code == 0:
            exit_code = 3
    return exit_code


def cmd_run(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    if args.isolation == "in-place":
        results = Orchestrator(root).run_workflow(args.feature_id, args.workflow)
        for execution in results:
            _print_step_execution(execution, root)
        return _workflow_exit_code(results)

    session = create_worktree_session(root, args.feature_id)
    print(
        f"Worktree isolation baseline branch={session.baseline.branch} "
        f"commit={session.baseline.commit} tree={session.baseline.tree}"
    )
    print(f"  worktree={session.worktree_path}")
    print(f"  branch={session.worktree_branch}")
    print(f"  evidence={session.evidence_path}")
    try:
        results = Orchestrator(session.worktree_path).run_workflow(args.feature_id, args.workflow)
        for execution in results:
            _print_step_execution(execution, session.worktree_path)
        exit_code = _workflow_exit_code(results)
        outcome = "failed" if exit_code == 2 else "paused" if exit_code == 3 else "success"
        cleanup = session.finalize(
            outcome,
            cleanup_requested=args.cleanup_worktree,
        )
        print(f"Worktree outcome={outcome} cleanup={cleanup}")
        if session.worktree_path.exists():
            print(f"Review isolated changes at {session.worktree_path}")
        return exit_code
    except KeyboardInterrupt:
        cleanup = session.finalize("cancelled", error="execution cancelled")
        print(f"Worktree outcome=cancelled cleanup={cleanup}", file=sys.stderr)
        raise
    except Exception as exc:
        cleanup = session.finalize("failed", error=str(exc))
        print(f"Worktree outcome=failed cleanup={cleanup}", file=sys.stderr)
        if session.worktree_path.exists():
            print(f"Preserved isolated worktree at {session.worktree_path}", file=sys.stderr)
        raise


def _step_detail(step) -> str:
    if step.action:
        return step.action
    if step.capability:
        semantic = f"agent={step.agent_name} " if step.agent_name else ""
        profile = f"profile={step.profile} " if step.profile else ""
        return f"{semantic}{profile}capability={step.capability.value}".strip()
    if step.quality_gate:
        return step.quality_gate
    if step.gate:
        return step.gate
    if step.children:
        return ",".join(child.id for child in step.children)
    return ""


def cmd_step(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    orchestrator = Orchestrator(root)
    definition = orchestrator.workflow_definition(args.workflow)
    context = orchestrator.context(args.feature_id)

    if args.step_action == "list":
        state = load_workflow_state(context, definition.name)
        for step, parent in definition.iter_steps():
            if state.is_complete(step.id):
                status = "completed"
            elif state.paused_at == step.id:
                status = "paused"
            else:
                status = "pending"
            condition = "" if step.condition == "always" else f" if={step.condition}"
            parent_text = f" parent={parent}" if parent else ""
            print(
                f"{step.id:26} type={step.kind.value:13} status={status:9} "
                f"{_step_detail(step)}{condition}{parent_text}"
            )
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
            agent_override=args.agent,
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
    record = grant_approval(
        context,
        args.gate,
        approved_by=args.by,
        role=args.role or "",
        note=args.note or "",
    )
    status = "satisfied" if record.satisfied else "pending"
    role = f" role={record.role}" if record.role else ""
    print(
        f"Approval recorded for gate '{record.gate}' on {args.feature_id} "
        f"by {record.approved_by}{role}; gate={status}; {record.detail}"
    )
    return 0 if record.satisfied else 3


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

    if args.agent_action == "definitions":
        for definition in list_agent_definitions(root):
            capabilities = ",".join(value.value for value in definition.capabilities)
            skills = ",".join(definition.skills) or "-"
            print(
                f"{definition.name:24} profile={definition.profile or '-':10} "
                f"mode={definition.execution_mode.value:15} capabilities={capabilities} skills={skills}"
            )
        return 0

    if args.agent_action == "show":
        definition = load_agent_definition(root, args.name)
        print(f"# {definition.name}\n\n{definition.description}\n")
        print(f"Capabilities: {', '.join(value.value for value in definition.capabilities)}")
        print(f"Default profile: {definition.profile or '-'}")
        print(f"Execution mode: {definition.execution_mode.value}")
        print(f"Skills: {', '.join(definition.skills) or '-'}\n")
        print(definition.instructions)
        return 0

    if args.agent_action == "doctor":
        exit_code = 0
        for name, provider, available, detail in runtime.doctor():
            status = "OK" if available else "MISSING"
            print(f"{status:7} {name:14} provider={provider:8} {detail}")
            if not available and detail != "profile disabled":
                exit_code = 1
        return exit_code

    if args.agent_action == "sync":
        changed = sync_native_agents(root, provider=args.provider, force=args.force)
        if not changed:
            print("Native agent files already synchronized")
            return 0
        print(f"Synchronized {len(changed)} native agent/skill path(s)")
        for path in changed:
            print(f"  + {path.relative_to(root)}")
        return 0

    if args.agent_action == "run":
        capability = Capability(args.capability)
        mode = ExecutionMode(args.mode)
        if args.dry_run:
            invocation = runtime.build_invocation(
                args.feature_id,
                capability,
                profile_name=args.profile,
                agent_name=args.agent,
                mode=mode,
            )
            print(
                f"agent={invocation.agent_name or '-'} profile={invocation.profile.name} "
                f"provider={invocation.profile.provider} capability={capability.value} mode={mode.value}"
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
            agent_name=args.agent,
            mode=mode,
        )
        print(
            f"[{result.agent_name or '-'}/{result.profile}/{result.provider}] "
            f"capability={result.capability.value} skills={','.join(result.skills) or '-'}"
        )
        print(result.output)
        return 0

    raise ValueError(f"Unknown agents action: {args.agent_action}")


def cmd_gates(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    gates = load_quality_gates(root)
    if args.gate_action == "list":
        for gate in gates.values():
            state = "enabled" if gate.enabled else "disabled"
            print(
                f"{gate.name:16} {state:8} timeout={gate.timeout_seconds}s "
                f"executable={gate.command[0]} args={max(0, len(gate.command) - 1)}"
            )
        return 0
    if args.gate_action == "run":
        context = Orchestrator(root).context(args.feature_id) if args.feature_id else None
        result = QualityGateRunner(root).run(args.name, context=context)
        print(f"gate={result.name} passed={result.passed} exit_code={result.return_code}")
        if result.artifact:
            print(f"  + {result.artifact.relative_to(root)}")
        if args.show_output and result.output:
            print(result.output.rstrip())
        return 0 if result.passed else 2
    raise ValueError(f"Unknown gate action: {args.gate_action}")


def cmd_policy(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    definition = Orchestrator(root).workflow_definition(args.workflow)
    findings = check_workflow_governance(root, definition)
    if not findings:
        print(f"Governance check passed for workflow '{definition.name}'")
        return 0
    for finding in findings:
        print(f"{finding.level:5} {finding.code}: {finding.message}")
    return 2 if any(item.level == "ERROR" for item in findings) else 0


def cmd_intake(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    if args.intake_source == "github":
        feature_id = validate_feature_id(args.feature_id)
        issue = GitHubCli(cwd=root).issue(args.repo, args.issue)
        content = github_issue_intake(issue, feature_id, args.workflow)
    elif args.intake_source == "jira":
        issue = JiraClient.from_env().issue(args.issue_key)
        feature_id = validate_feature_id(args.feature_id or issue.key)
        content = jira_issue_intake(issue, feature_id, args.workflow)
    else:
        raise ValueError(f"Unknown intake source: {args.intake_source}")
    context = FeatureContext(root, feature_id)
    write_text(context.artifact("00-intake.md"), content, overwrite=False)
    print(f"Created specs/{feature_id}/00-intake.md from {args.intake_source}")
    return 0


def _current_git_branch(root: Path) -> str:
    completed = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    branch = (completed.stdout or "").strip()
    if completed.returncode != 0 or not branch:
        raise RuntimeError("Unable to determine current Git branch; pass --head explicitly")
    return branch


def _feature_title(feature_dir: Path, feature_id: str) -> str:
    intake = feature_dir / "00-intake.md"
    if not intake.exists():
        return feature_id
    lines = read_utf8_text(intake).splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "## Title" and index + 1 < len(lines):
            for candidate in lines[index + 1 :]:
                if candidate.strip():
                    return candidate.strip()
    return feature_id


def cmd_pr(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    context = FeatureContext(root, args.feature_id)
    feature_dir = context.feature_dir
    if not context.artifact("00-intake.md").exists():
        raise FileNotFoundError(f"Feature intake not found for {context.feature_id}")
    head = args.head or _current_git_branch(root)
    title = args.title or _feature_title(feature_dir, context.feature_id)
    body = build_pull_request_body(feature_dir, context.feature_id)
    url = GitHubCli(cwd=root).create_pull_request(
        PullRequestRequest(
            repository=args.repo,
            base=args.base,
            head=head,
            title=title,
            body=body,
            draft=not args.ready,
        )
    )
    print(url)
    return 0


def cmd_integrations(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    config_path = root / ".sdai" / "integrations.yaml"
    config = load_yaml(config_path) if config_path.exists() else {}
    exit_code = 0

    github_config = config.get("github") or {}
    if bool(github_config.get("enabled", True)):
        gh_ok, gh_detail = GitHubCli(cwd=root).availability()
        print(f"{'OK' if gh_ok else 'MISSING':8} github  {gh_detail}")
        if not gh_ok:
            exit_code = 1
    else:
        print("DISABLED github  disabled by .sdai/integrations.yaml")

    jira_config = config.get("jira") or {}
    if bool(jira_config.get("enabled", False)):
        jira_base = os.getenv("JIRA_BASE_URL", "")
        jira_auth = bool(
            os.getenv("JIRA_BEARER_TOKEN")
            or (os.getenv("JIRA_EMAIL") and os.getenv("JIRA_API_TOKEN"))
        )
        jira_ok = jira_base.startswith("https://") and jira_auth
        print(
            f"{'OK' if jira_ok else 'MISSING':8} jira    "
            f"{'environment configured' if jira_ok else 'set HTTPS JIRA_BASE_URL and Jira credentials'}"
        )
        if not jira_ok:
            exit_code = 1
    else:
        print("DISABLED jira    disabled by .sdai/integrations.yaml")
    return exit_code


def cmd_skills(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    ensure_initialized(root)
    if args.skill_action == "list":
        for skill in list_skills(root):
            capabilities = ",".join(value.value for value in skill.capabilities) or "all"
            source = ".agents/skills" if ".agents" in skill.root.parts else ".sdai/skills"
            print(f"{skill.name:24} source={source:14} capabilities={capabilities}\n  {skill.description}")
        return 0
    if args.skill_action == "show":
        skill = load_skill(root, args.name)
        print(f"# {skill.name}\n\n{skill.description}\n\n{skill.instructions}")
        return 0
    if args.skill_action == "validate":
        names = validate_skills(root)
        print(f"Validated {len(names)} skill(s): {', '.join(names) or '-'}")
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

    upgrade = sub.add_parser("upgrade", help="Add missing files for the current SD-AI scaffold")
    upgrade.add_argument("--path")
    upgrade.set_defaults(func=cmd_upgrade)

    feature = sub.add_parser("feature", help="Create a feature intake")
    feature.add_argument("feature_id")
    feature.add_argument("--title", required=True)
    feature.add_argument("--description", required=True)
    feature.add_argument("--workflow", default="standard")
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

    run = sub.add_parser("run", help="Execute or resume a declarative workflow")
    run.add_argument("feature_id")
    run.add_argument("--workflow", default="standard")
    run.add_argument("--isolation", choices=["in-place", "worktree"], default="in-place")
    run.add_argument(
        "--cleanup-worktree",
        action="store_true",
        help="Remove the isolated worktree after success only when it is clean",
    )
    run.add_argument("--path")
    run.set_defaults(func=cmd_run)

    step = sub.add_parser("step", help="List or manually run any workflow step")
    step_sub = step.add_subparsers(dest="step_action", required=True)
    step_list = step_sub.add_parser("list")
    step_list.add_argument("feature_id")
    step_list.add_argument("--workflow", default="standard")
    step_list.add_argument("--path")
    step_list.set_defaults(func=cmd_step)
    step_run = step_sub.add_parser("run")
    step_run.add_argument("feature_id")
    step_run.add_argument("step_id")
    step_run.add_argument("--workflow", default="standard")
    step_run.add_argument("--force", action="store_true")
    step_run.add_argument("--dry-run", action="store_true")
    step_run.add_argument("--agent", help="Override the semantic .agent role")
    step_run.add_argument("--profile", help="Override the provider profile")
    step_run.add_argument("--mode", choices=[m.value for m in ExecutionMode])
    step_run.add_argument("--path")
    step_run.set_defaults(func=cmd_step)

    approve = sub.add_parser("approve")
    approve.add_argument("feature_id")
    approve.add_argument("gate")
    approve.add_argument("--by", required=True)
    approve.add_argument("--role")
    approve.add_argument("--note")
    approve.add_argument("--path")
    approve.set_defaults(func=cmd_approve)

    agents = sub.add_parser("agents", help="Profiles, semantic agents, native sync, and execution")
    agents_sub = agents.add_subparsers(dest="agent_action", required=True)
    for action in ("list", "definitions", "doctor"):
        item = agents_sub.add_parser(action)
        item.add_argument("--path")
        item.set_defaults(func=cmd_agents)
    agents_show = agents_sub.add_parser("show")
    agents_show.add_argument("name")
    agents_show.add_argument("--path")
    agents_show.set_defaults(func=cmd_agents)
    agents_sync = agents_sub.add_parser("sync")
    agents_sync.add_argument("--provider", choices=["all", *PROVIDERS], default="all")
    agents_sync.add_argument("--force", action="store_true")
    agents_sync.add_argument("--path")
    agents_sync.set_defaults(func=cmd_agents)
    agents_run = agents_sub.add_parser("run")
    agents_run.add_argument("capability", choices=[c.value for c in Capability])
    agents_run.add_argument("feature_id")
    agents_run.add_argument("--agent", help="Semantic agent definition name")
    agents_run.add_argument("--profile", help="Provider profile override")
    agents_run.add_argument("--mode", choices=[m.value for m in ExecutionMode], default="advisory")
    agents_run.add_argument("--dry-run", action="store_true")
    agents_run.add_argument("--path")
    agents_run.set_defaults(func=cmd_agents)

    gates = sub.add_parser("gates")
    gates_sub = gates.add_subparsers(dest="gate_action", required=True)
    gates_list = gates_sub.add_parser("list")
    gates_list.add_argument("--path")
    gates_list.set_defaults(func=cmd_gates)
    gates_run = gates_sub.add_parser("run")
    gates_run.add_argument("name")
    gates_run.add_argument("--feature-id")
    gates_run.add_argument("--show-output", action="store_true")
    gates_run.add_argument("--path")
    gates_run.set_defaults(func=cmd_gates)

    policy = sub.add_parser("policy")
    policy_sub = policy.add_subparsers(dest="policy_action", required=True)
    policy_check = policy_sub.add_parser("check")
    policy_check.add_argument("--workflow", default="enterprise")
    policy_check.add_argument("--path")
    policy_check.set_defaults(func=cmd_policy)

    intake = sub.add_parser("intake")
    intake_sub = intake.add_subparsers(dest="intake_source", required=True)
    intake_github = intake_sub.add_parser("github")
    intake_github.add_argument("feature_id")
    intake_github.add_argument("--repo", required=True)
    intake_github.add_argument("--issue", type=int, required=True)
    intake_github.add_argument("--workflow", default="enterprise")
    intake_github.add_argument("--path")
    intake_github.set_defaults(func=cmd_intake)
    intake_jira = intake_sub.add_parser("jira")
    intake_jira.add_argument("issue_key")
    intake_jira.add_argument("--feature-id")
    intake_jira.add_argument("--workflow", default="enterprise")
    intake_jira.add_argument("--path")
    intake_jira.set_defaults(func=cmd_intake)

    pr = sub.add_parser("pr")
    pr_sub = pr.add_subparsers(dest="pr_action", required=True)
    pr_create = pr_sub.add_parser("create")
    pr_create.add_argument("feature_id")
    pr_create.add_argument("--repo", required=True)
    pr_create.add_argument("--base", default="main")
    pr_create.add_argument("--head")
    pr_create.add_argument("--title")
    pr_create.add_argument("--ready", action="store_true")
    pr_create.add_argument("--path")
    pr_create.set_defaults(func=cmd_pr)

    integrations = sub.add_parser("integrations")
    integrations_sub = integrations.add_subparsers(dest="integration_action", required=True)
    integrations_doctor = integrations_sub.add_parser("doctor")
    integrations_doctor.add_argument("--path")
    integrations_doctor.set_defaults(func=cmd_integrations)

    skills = sub.add_parser("skills", help="List, inspect, or validate shared skills")
    skills_sub = skills.add_subparsers(dest="skill_action", required=True)
    skills_list = skills_sub.add_parser("list")
    skills_list.add_argument("--path")
    skills_list.set_defaults(func=cmd_skills)
    skills_show = skills_sub.add_parser("show")
    skills_show.add_argument("name")
    skills_show.add_argument("--path")
    skills_show.set_defaults(func=cmd_skills)
    skills_validate = skills_sub.add_parser("validate")
    skills_validate.add_argument("--path")
    skills_validate.set_defaults(func=cmd_skills)

    prompts = sub.add_parser("prompts")
    prompts_sub = prompts.add_subparsers(dest="prompt_action", required=True)
    prompts_list = prompts_sub.add_parser("list")
    prompts_list.add_argument("--path")
    prompts_list.set_defaults(func=cmd_prompts)
    prompts_show = prompts_sub.add_parser("show")
    prompts_show.add_argument("name")
    prompts_show.add_argument("--path")
    prompts_show.set_defaults(func=cmd_prompts)

    add_store_parser(sub)
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