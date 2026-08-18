from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sdai.contract_adapters import default_contract_registry
from sdai.contract_gate import evaluate_contract_gate
from sdai.contract_policy import ContractCriticality, contract_policy_exit_code
from sdai.contracts import (
    CompatibilityDirection,
    ContractAdapterRegistry,
    ContractError,
    ContractInspection,
    ContractSnapshot,
    check_contract,
    diff_contracts,
    discover_contracts,
    load_explicit_snapshot,
)
from sdai.trace_freshness import CommitPolicy


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", help="SD-AI project root")
    parser.add_argument(
        "--manifest",
        default=".sdai/contracts.yaml",
        help="Project-relative explicit contract source manifest",
    )


def _add_diff_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source")
    parser.add_argument("--against", required=True, help="Project-relative local comparison source")
    parser.add_argument(
        "--direction",
        choices=[item.value for item in CompatibilityDirection if item is not CompatibilityDirection.NONE],
        default=CompatibilityDirection.BACKWARD.value,
    )
    _add_common_arguments(parser)


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
    _add_diff_arguments(diff)
    diff.add_argument("--json", action="store_true")

    gate = actions.add_parser(
        "gate",
        help="Apply deterministic contract policy, constitution, and fresh evidence gates",
    )
    _add_diff_arguments(gate)
    gate.add_argument(
        "--criticality",
        choices=[item.value for item in ContractCriticality],
        default=ContractCriticality.CRITICAL.value,
    )
    gate.add_argument(
        "--evidence",
        action="append",
        default=[],
        metavar="PATH",
        help="Repository-local canonical trace evidence file; repeat for multiple records",
    )
    gate.add_argument(
        "--evidence-commit-policy",
        choices=[item.value for item in CommitPolicy],
        default=CommitPolicy.ANCESTOR.value,
        help="Git freshness rule for governance evidence",
    )
    gate.add_argument("--json", action="store_true")
    return parser


def _root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def _ensure_initialized(root: Path) -> None:
    if not (root / ".sdai" / "config.yaml").is_file():
        raise ContractError("SDAI-CONTRACT-PROJECT-001", "Not an SD-AI project. Run `sdai init` first.")


def _select_source(inspection: ContractInspection, source_id: str) -> ContractSnapshot:
    for snapshot in inspection.sources:
        if snapshot.source.source_id == source_id:
            return snapshot
    raise ContractError("SDAI-CONTRACT-SOURCE-010", f"contract source '{source_id}' is not declared")


def _comparison_snapshot(root: Path, snapshot: ContractSnapshot, against: str) -> ContractSnapshot:
    return load_explicit_snapshot(
        root,
        source_id=snapshot.source.source_id,
        kind=snapshot.source.kind,
        path=against,
    )


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
        inspection = discover_contracts(root, args.manifest)

        if args.action == "inspect":
            if as_json:
                sys.stdout.write(inspection.to_json())
            else:
                print(
                    f"Contract sources={len(inspection.sources)} manifest={inspection.manifest_sha256} "
                    f"snapshot={inspection.sha256}"
                )
                for item in inspection.sources:
                    print(
                        f"  {item.source.source_id} kind={item.source.kind} path={item.source.path} "
                        f"sha256={item.sha256} bytes={item.size_bytes}"
                    )
            return 0

        adapters = registry if registry is not None else default_contract_registry(inspection.sources)
        snapshot = _select_source(inspection, args.source)
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

        if args.action in {"diff", "gate"}:
            after = _comparison_snapshot(root, snapshot, args.against)
            direction = CompatibilityDirection(args.direction)
            result = diff_contracts(snapshot, after, adapters, direction)
            if args.action == "diff":
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

            decision = evaluate_contract_gate(
                root,
                result,
                criticality=ContractCriticality(args.criticality),
                evidence_paths=args.evidence,
                commit_policy=CommitPolicy(args.evidence_commit_policy),
            )
            if as_json:
                sys.stdout.write(decision.to_json())
            else:
                print(
                    f"Contract gate source={snapshot.source.source_id} direction={direction.value} "
                    f"criticality={decision.criticality.value} class={decision.change_class.value} "
                    f"outcome={decision.outcome.value} result={decision.sha256}"
                )
                for reason in decision.reasons:
                    print(f"  {reason}")
                for item in decision.evidence:
                    status = "accepted" if item.accepted else "rejected"
                    evidence_type = item.evidence_type.value if item.evidence_type is not None else "unknown"
                    print(
                        f"  evidence {item.evidence_id} type={evidence_type} "
                        f"freshness={item.freshness} status={status}"
                    )
            return contract_policy_exit_code(decision)

        raise ContractError("SDAI-CONTRACT-CLI-001", f"unknown contract action '{args.action}'")
    except ContractError as exc:
        return _print_error(exc, as_json=as_json)


if __name__ == "__main__":
    raise SystemExit(main())
