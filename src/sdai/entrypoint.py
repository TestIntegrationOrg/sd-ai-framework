from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sdai.cli import main as legacy_main, parser as legacy_parser
from sdai.constitution import (
    check_constitution,
    init_constitution,
    load_constitution,
    write_constitution_evidence,
)
from sdai.evals import MockEvalExecutor, run_behavioral_eval
from sdai.extensions.scaffolding import (
    ScaffoldKind,
    create_extension_scaffold,
    validate_extension_scaffold,
)
from sdai.requirements_quality import (
    analyze_clarifications,
    write_clarifications,
    write_requirements_checklist,
)
from sdai.spec_cli import add_spec_parser, run_spec_command
from sdai.text import read_utf8_text


def _root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def _ensure_initialized(root: Path) -> None:
    if not (root / ".sdai" / "config.yaml").exists():
        raise RuntimeError("Not an SD-AI project. Run `sdai init` first.")


def _add_eval_parser(
    commands: argparse._SubParsersAction,
    target_type: str,
) -> None:
    target = commands.add_parser(
        target_type,
        help=f"Evaluate a {target_type} with behavioral scenarios",
    )
    target_sub = target.add_subparsers(
        dest=f"{target_type}_action",
        required=True,
    )
    evaluate = target_sub.add_parser("eval")
    evaluate.add_argument("name")
    evaluate.add_argument("--provider", choices=["mock"], default="mock")
    evaluate.add_argument(
        "--require-improvement",
        action="store_true",
        help="Fail unless aggregate candidate score is strictly better than baseline",
    )
    evaluate.add_argument("--json", action="store_true")
    evaluate.add_argument("--path")


def _managed_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdai")
    commands = parser.add_subparsers(dest="managed_command", required=True)

    create = commands.add_parser(
        "create",
        help="Create a minimal valid SDAI extension scaffold",
    )
    create.add_argument("kind", choices=[item.value for item in ScaffoldKind])
    create.add_argument("name")
    create.add_argument("--path")
    create.add_argument(
        "--force",
        action="store_true",
        help="Explicitly replace files owned by this scaffold",
    )

    extensions = commands.add_parser(
        "extensions",
        aliases=["extension"],
        help="Validate and inspect SDAI extensions",
    )
    extension_sub = extensions.add_subparsers(dest="extension_action", required=True)
    validate = extension_sub.add_parser("validate")
    validate.add_argument("kind", choices=[item.value for item in ScaffoldKind])
    validate.add_argument("target", help="Canonical name or extension manifest path")
    validate.add_argument("--path")

    constitution = commands.add_parser(
        "constitution",
        help="Manage the repository engineering constitution",
    )
    constitution_sub = constitution.add_subparsers(
        dest="constitution_action", required=True
    )
    constitution_init = constitution_sub.add_parser("init")
    constitution_init.add_argument("--path")
    constitution_init.add_argument("--force", action="store_true")
    constitution_show = constitution_sub.add_parser("show")
    constitution_show.add_argument("--path")
    constitution_validate = constitution_sub.add_parser("validate")
    constitution_validate.add_argument("--path")
    constitution_check = constitution_sub.add_parser("check")
    constitution_check.add_argument("feature")
    constitution_check.add_argument("--path")

    clarify = commands.add_parser(
        "clarify",
        help="Generate reviewer-owned requirements clarification questions",
    )
    clarify.add_argument("feature")
    clarify.add_argument("--path")

    requirements = commands.add_parser(
        "requirements",
        help="Run deterministic requirements quality checks",
    )
    requirements_sub = requirements.add_subparsers(
        dest="requirements_action", required=True
    )
    requirements_check = requirements_sub.add_parser("check")
    requirements_check.add_argument("feature")
    requirements_check.add_argument("--path")

    _add_eval_parser(commands, "skill")
    _add_eval_parser(commands, "agent")
    add_spec_parser(commands)
    return parser


def _portable_relative(path: Path, root: Path) -> str:
    """Render repository paths consistently across Windows, macOS, and Linux."""

    return path.relative_to(root).as_posix()


def _run_eval(root: Path, target_type: str, args: argparse.Namespace) -> int:
    if args.provider != "mock":
        raise ValueError(
            f"Behavioral eval provider '{args.provider}' is not supported in v1; use mock"
        )
    report = run_behavioral_eval(
        root,
        target_type,
        args.name,
        executor=MockEvalExecutor(),
        require_improvement=args.require_improvement,
    )
    if args.json:
        print(report.to_json())
    else:
        print(
            f"{target_type.capitalize()} eval target={args.name} "
            f"provider={report.provider} model={report.model} "
            f"baseline={report.baseline_score:.2f} candidate={report.candidate_score:.2f} "
            f"delta={report.delta:+.2f} passed={str(report.passed).lower()}"
        )
        for result in report.scenarios:
            status = "PASS" if result.passed else "FAIL"
            regression = " regression" if result.regression else ""
            print(
                f"  {status:4} {result.id} baseline={result.baseline_score:.2f} "
                f"candidate={result.candidate_score:.2f} delta={result.delta:+.2f}{regression}"
            )
    return 0 if report.passed else 1


def _run_managed_command(argv: list[str]) -> int:
    args = _managed_parser().parse_args(argv)
    root = _root(getattr(args, "path", None))
    _ensure_initialized(root)

    if args.managed_command == "create":
        result = create_extension_scaffold(
            root,
            args.kind,
            args.name,
            force=args.force,
        )
        print(f"Created {result.kind.value} '{result.id}'")
        for path in result.paths:
            print(f"  + {_portable_relative(path, root)}")
        return 0

    if args.managed_command in {"extension", "extensions"}:
        if args.extension_action == "validate":
            manifest = validate_extension_scaffold(root, args.kind, args.target)
            detail = ""
            if manifest is not None:
                detail = f" kind={manifest.kind.value} version={manifest.metadata.version}"
            print(f"Validated {args.kind} '{args.target}'{detail}")
            return 0

    if args.managed_command == "constitution":
        if args.constitution_action == "init":
            path = init_constitution(root, force=args.force)
            constitution = load_constitution(root)
            print(
                f"Initialized engineering constitution at {_portable_relative(path, root)} "
                f"sha256={constitution.sha256}"
            )
            return 0
        if args.constitution_action == "show":
            constitution = load_constitution(root)
            print(read_utf8_text(constitution.path), end="")
            return 0
        if args.constitution_action == "validate":
            constitution = load_constitution(root)
            print(
                f"Validated engineering constitution principles={len(constitution.principles)} "
                f"sha256={constitution.sha256}"
            )
            return 0
        if args.constitution_action == "check":
            findings = check_constitution(root, args.feature)
            evidence_path = write_constitution_evidence(root, args.feature)
            blocking = [
                finding
                for finding in findings
                if finding.severity == "blocking" and finding.status == "fail"
            ]
            review = [finding for finding in findings if finding.status == "review"]
            print(
                f"Constitution check feature={args.feature} blocking_failures={len(blocking)} "
                f"review_required={len(review)} evidence={_portable_relative(evidence_path, root)}"
            )
            return 1 if blocking else 0

    if args.managed_command == "clarify":
        findings = analyze_clarifications(root, args.feature)
        path = write_clarifications(root, args.feature)
        open_count = sum(1 for finding in findings if finding.status == "open")
        print(
            f"Clarification review feature={args.feature} open={open_count} "
            f"total={len(findings)} artifact={_portable_relative(path, root)}"
        )
        return 0

    if args.managed_command == "requirements":
        if args.requirements_action == "check":
            path, report = write_requirements_checklist(root, args.feature)
            print(
                f"Requirements check feature={args.feature} "
                f"blocking_failures={len(report.blocking_failures)} "
                f"warnings={len(report.warning_failures)} "
                f"artifact={_portable_relative(path, root)}"
            )
            return 1 if report.blocking_failures else 0

    if args.managed_command == "skill" and args.skill_action == "eval":
        return _run_eval(root, "skill", args)

    if args.managed_command == "agent" and args.agent_action == "eval":
        return _run_eval(root, "agent", args)

    if args.managed_command == "spec":
        return run_spec_command(root, args)

    raise ValueError(f"Unknown managed command: {args.managed_command}")


def _print_top_level_help() -> None:
    print(legacy_parser().format_help().rstrip())
    print(
        "\nExtension authoring commands:\n"
        "  sdai create <kind> <name> [--path PATH] [--force]\n"
        "  sdai extensions validate <kind> <name-or-manifest> [--path PATH]\n"
        "\nRequirements quality commands:\n"
        "  sdai constitution init|show|validate|check ...\n"
        "  sdai clarify <feature> [--path PATH]\n"
        "  sdai requirements check <feature> [--path PATH]\n"
        "\nBehavioral evaluation commands:\n"
        "  sdai skill eval <name> [--provider mock] [--require-improvement] [--json]\n"
        "  sdai agent eval <name> [--provider mock] [--require-improvement] [--json]\n"
        "\nCurrent specification commands:\n"
        "  sdai spec validate <feature> [--json]\n"
        "  sdai spec diff <feature> [--json] [--include-content]\n"
        "  sdai spec approve <feature> --by <identity> [--role ROLE] [--note NOTE]\n"
        "  sdai spec promote <feature> [--dry-run] [--json] [--include-content]\n"
        "\nExtension kinds: "
        + ", ".join(item.value for item in ScaffoldKind)
    )


def main(argv: list[str] | None = None) -> int:
    effective = list(sys.argv[1:] if argv is None else argv)
    if effective and effective[0] in {"-h", "--help"}:
        _print_top_level_help()
        return 0
    if effective and effective[0] in {
        "create",
        "extension",
        "extensions",
        "constitution",
        "clarify",
        "requirements",
        "skill",
        "agent",
        "spec",
    }:
        try:
            return _run_managed_command(effective)
        except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return legacy_main(effective)


if __name__ == "__main__":
    raise SystemExit(main())
