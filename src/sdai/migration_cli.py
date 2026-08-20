from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sdai import __version__
from sdai.migration import MigrationError
from sdai.migration_transaction import (
    apply_migration,
    plan_migration,
    rollback_migration,
)


_FRAMEWORK_METADATA_PATH = ".sdai/framework-version.yaml"


def _root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdai migrate",
        description="Preview, apply, and safely rollback SDAI scaffold migrations",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    plan = actions.add_parser("plan", help="Preview the exact non-destructive upgrade delta")
    plan.add_argument("--json", action="store_true")
    plan.add_argument("--path")

    apply = actions.add_parser("apply", help="Apply the planned scaffold migration safely")
    apply.add_argument("--json", action="store_true")
    apply.add_argument("--path")

    rollback = actions.add_parser(
        "rollback",
        help="Rollback a recorded migration if migrated bytes are still unchanged",
    )
    rollback.add_argument("migration_id")
    rollback.add_argument("--json", action="store_true")
    rollback.add_argument("--path")
    return parser


def _upgrade_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdai upgrade",
        description="Add missing/current stock files using the safe migration engine",
    )
    parser.add_argument("--path")
    return parser


def _run_plan(args: argparse.Namespace) -> int:
    root = _root(args.path)
    plan = plan_migration(root)
    if args.json:
        sys.stdout.write(plan.to_json())
        return 0
    state = "current" if plan.current else "upgrade-required"
    print(
        f"Migration plan state={state} changes={len(plan.changes)} "
        f"sha256={plan.sha256}"
    )
    for change in plan.changes:
        before = change.before_sha256 or "<absent>"
        print(
            f"  {change.action:13} {change.path} "
            f"before={before} after={change.after_sha256}"
        )
    return 0


def _run_apply(args: argparse.Namespace) -> int:
    root = _root(args.path)
    result = apply_migration(root)
    if args.json:
        sys.stdout.write(result.to_json())
        return 0
    if result.status == "current":
        print("SD-AI project already has the current scaffold")
        return 0
    print(
        f"Applied SD-AI migration id={result.migration_id} "
        f"changes={len(result.changes)} plan={result.plan_sha256}"
    )
    for change in result.changes:
        print(f"  {change.action:13} {change.path}")
    print(f"  manifest={result.manifest_path}")
    return 0


def _run_rollback(args: argparse.Namespace) -> int:
    root = _root(args.path)
    result = rollback_migration(root, args.migration_id)
    if args.json:
        sys.stdout.write(result.to_json())
        return 0
    print(
        f"Migration rollback id={result.migration_id} status={result.status} "
        f"changes={len(result.changes)} plan={result.plan_sha256}"
    )
    for change in result.changes:
        print(f"  {change['action']:13} {change['path']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "plan":
            return _run_plan(args)
        if args.action == "apply":
            return _run_apply(args)
        if args.action == "rollback":
            return _run_rollback(args)
        raise ValueError(f"Unknown migration action: {args.action}")
    except (FileNotFoundError, MigrationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def upgrade_main(argv: list[str] | None = None) -> int:
    """Backward-compatible public `sdai upgrade` routed through safe migration."""

    args = _upgrade_parser().parse_args(argv)
    root = _root(args.path)
    try:
        result = apply_migration(root)
    except (FileNotFoundError, MigrationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    scaffold_changes = tuple(
        change for change in result.changes if change.path != _FRAMEWORK_METADATA_PATH
    )
    if not scaffold_changes:
        print("SD-AI project already has the current scaffold")
    else:
        print(f"Upgraded SD-AI project at {root}")
        for change in scaffold_changes:
            print(f"  + {change.path}")

    # Preserve the installed entrypoint's historical release-metadata footer.
    # The migration engine tracks this file internally so rollback can restore it,
    # but legacy upgrade output still reports it exactly once in the footer.
    print(f"SD-AI framework version {__version__}")
    print(f"  + {_FRAMEWORK_METADATA_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
