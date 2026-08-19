from __future__ import annotations

from collections.abc import Callable

from sdai.audited_orchestrator import AuditedOrchestrator
from sdai.workflow_machine_audit import audited_resume_workflow_run


def run_lifecycle_with_workflow_audit(
    main: Callable[[list[str]], int],
    argv: list[str],
) -> int:
    """Run lifecycle parsing with legacy and Engine 2 workflow audit enabled."""
    from sdai import cli as lifecycle_cli
    from sdai import workflow_cli

    original_orchestrator = lifecycle_cli.Orchestrator
    original_resume = workflow_cli.resume_workflow_run
    lifecycle_cli.Orchestrator = AuditedOrchestrator
    workflow_cli.resume_workflow_run = audited_resume_workflow_run
    try:
        return main(argv)
    finally:
        workflow_cli.resume_workflow_run = original_resume
        lifecycle_cli.Orchestrator = original_orchestrator


def run_multi_repo_with_workflow_audit(
    main: Callable[[list[str]], int],
    argv: list[str],
) -> int:
    """Run multi-repository execution with the same audited orchestration facade."""
    from sdai import multi_repo_run_cli

    original = multi_repo_run_cli.Orchestrator
    multi_repo_run_cli.Orchestrator = AuditedOrchestrator
    try:
        return main(argv)
    finally:
        multi_repo_run_cli.Orchestrator = original


__all__ = [
    "run_lifecycle_with_workflow_audit",
    "run_multi_repo_with_workflow_audit",
]
