from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sdai.cli import main as legacy_main
from sdai.extensions.scaffolding import (
    ScaffoldKind,
    create_extension_scaffold,
    validate_extension_scaffold,
)


def _root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def _ensure_initialized(root: Path) -> None:
    if not (root / ".sdai" / "config.yaml").exists():
        raise RuntimeError("Not an SD-AI project. Run `sdai init` first.")


def _extension_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdai")
    commands = parser.add_subparsers(dest="extension_command", required=True)

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
    return parser


def _run_extension_command(argv: list[str]) -> int:
    args = _extension_parser().parse_args(argv)
    root = _root(args.path)
    _ensure_initialized(root)

    if args.extension_command == "create":
        result = create_extension_scaffold(
            root,
            args.kind,
            args.name,
            force=args.force,
        )
        print(f"Created {result.kind.value} '{result.id}'")
        for path in result.paths:
            print(f"  + {path.relative_to(root)}")
        return 0

    if args.extension_command in {"extension", "extensions"}:
        if args.extension_action == "validate":
            manifest = validate_extension_scaffold(root, args.kind, args.target)
            detail = ""
            if manifest is not None:
                detail = f" kind={manifest.kind.value} version={manifest.metadata.version}"
            print(f"Validated {args.kind} '{args.target}'{detail}")
            return 0

    raise ValueError(f"Unknown extension command: {args.extension_command}")


def main(argv: list[str] | None = None) -> int:
    effective = list(sys.argv[1:] if argv is None else argv)
    if effective and effective[0] in {"create", "extension", "extensions"}:
        try:
            return _run_extension_command(effective)
        except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return legacy_main(effective)


if __name__ == "__main__":
    raise SystemExit(main())
