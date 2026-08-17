from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sdai.contracts import (
    CompatibilityDirection,
    ContractAdapterRegistry,
    ContractError,
    check_contract,
    diff_contracts,
    discover_contracts,
    find_contract_source,
    load_explicit_snapshot,
)


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", help="SD-AI project root")
    parser.add_argument(
        "--manifest",
        default=".sdai/contracts.yaml",
        help="Project-relative explicit contract source manifest",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdai contract",
        description="Inspect and evaluate explicitly declared local API/data contracts",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    inspect = actions.add_parser("inspect", help="Discover and hash declared local contracts")
    _add_common_arguments(inspect)
    inspect.add_argument("--json", action="store_true")

    check = actions.add_parser("check", help="Validate one declared contract through its format adapter")
    check.add_argument("source")
    _add_common_arguments(check)
    check.add_argument("--json", action="store_true")

    diff = actions.add_parser("diff", help="Compare a declared contract with an explicit local source")
    diff.add_argument("source")
    diff.add_argument("--against", required=True, help="Project-relative local comparison source")
    diff.add_argument(
        "--direction",
        choices=[item.value for item in CompatibilityDirection if item is not CompatibilityDirection.NONE],
        default=CompatibilityDirection.BACKWARD.value,
    )
    _add_common_arguments(diff)
    diff.add_argument("--json", action="store_true")
    return parser


def _root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def _ensure_initialized(root: Path) -> None:
    if not (root / ".sdai" / "config.yaml").is_file():
        raise ContractError("SDAI-CONTRACT-PROJECT-001", "Not an SD-AI project. Run `sdai init` first.")


def _print_error(error: ContractError, *, as_json: bool) -> int:
    if as_json:
        sys.stdout.write(error.to_json())
    else:
        print(f"error: {error}", file=sys.stderr)
    return 1


def main(
    argv: list[str] | None = None,
    *,
    registry: ContractAdapterRegistry | None = None,
) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    as_json = bool(getattr(args, "json", False))
    try:
        root = _root(args.path)
        _ensure_initialized(root)
        adapters = registry or ContractAdapterRegistry()

        if args.action == "inspect":
            result = discover_contracts(root, args.manifest)
            if as_json:
                sys.stdout.write(result.to_json())
            else:
                print(
                    f"Contract sources={len(result.sources)} manifest={result.manifest_sha256} "
                    f"snapshot={result.sha256}"
                )
                for item in result.sources:
                    print(
                        f"  {item.source.source_id} kind={item.source.kind} path={item.source.path} "
                        f"sha256={item.sha256} bytes={item.size_bytes}"
                    )
            return 0

        snapshot = find_contract_source(root, args.source, args.manifest)
        if args.action == "check":
            result = check_contract(snapshot, adapters)
            if as_json:
                sys.stdout.write(result.to_json())
            else:
                print(
                    f"Contract check source={snapshot.source.source_id} valid={str(result.valid).lower()} "
                    f"findings={len(result.findings)} result={result.sha256}"
                )
                for finding in result.findings:
                    print(f"  {finding.severity.value:7} {finding.code}: {finding.message}")
            return 0 if result.valid else 1

        if args.action == "diff":
            after = load_explicit_snapshot(
                root,
                source_id=snapshot.source.source_id,
                kind=snapshot.source.kind,
                path=args.against,
            )
            direction = CompatibilityDirection(args.direction)
            result = diff_contracts(snapshot, after, adapters, direction)
            if as_json:
                sys.stdout.write(result.to_json())
            else:
                print(
                    f"Contract diff source={snapshot.source.source_id} direction={direction.value} "
                    f"compatible={str(result.compatible).lower()} findings={len(result.findings)} "
                    f"result={result.sha256}"
                )
                for finding in result.findings:
                    print(f"  {finding.severity.value:7} {finding.code}: {finding.message}")
            return 0 if result.compatible else 1

        raise ContractError("SDAI-CONTRACT-CLI-001", f"unknown contract action '{args.action}'")
    except ContractError as exc:
        return _print_error(exc, as_json=as_json)


if __name__ == "__main__":
    raise SystemExit(main())
