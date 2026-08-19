from __future__ import annotations

from collections.abc import Callable

from sdai.audited_orchestrator import AuditedOrchestrator


def run_lifecycle_with_workflow_audit(
    main: Callable[[list[str]], int],
    argv: list[str],
) -> int:
    """Run legacy lifecycle parsing with only its Orchestrator binding replaced."""
    from sdai import cli as lifecycle_cli

    original = lifecycle_cli.Orchestrator
    lifecycle_cli.Orchestrator = AuditedOrchestrator
    try:
        return main(argv)
    finally:
        lifecycle_cli.Orchestrator = original


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
