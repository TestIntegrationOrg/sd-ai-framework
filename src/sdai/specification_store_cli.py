from __future__ import annotations

import argparse
from pathlib import Path

from sdai.specification_store_lifecycle import (
    StoreAutomationExit,
    create_store,
    doctor_stores,
    export_store_context,
    list_stores,
    register_store,
)


def _root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def _print_json_or_human(args: argparse.Namespace, payload, human_lines: tuple[str, ...]) -> None:
    if args.json:
        print(payload.to_json())
        return
    for line in human_lines:
        print(line)


def cmd_store(args: argparse.Namespace) -> int:
    action = args.store_action
    if action == "create":
        result = create_store(
            Path(args.destination),
            args.store_id,
            args.version,
            description=args.description,
        )
        verb = "Created" if result.created else "Already exists"
        _print_json_or_human(
            args,
            result,
            (
                f"{verb} SpecificationStore {result.identity}",
                f"manifest={result.manifest_sha256}",
            ),
        )
        return int(StoreAutomationExit.SUCCESS)

    root = _root(args.path)
    if action == "register":
        result = register_store(root, Path(args.store_path))
        verb = "Registered" if result.registered else "Already registered"
        _print_json_or_human(
            args,
            result,
            (
                f"{verb} SpecificationStore {result.identity}",
                f"manifest={result.manifest_sha256} scope={result.path_scope}",
            ),
        )
        return int(StoreAutomationExit.SUCCESS)
    if action == "list":
        result = list_stores(root)
        if args.json:
            print(result.to_json())
        elif not result.stores:
            print("No SpecificationStores registered")
        else:
            for item in result.stores:
                print(
                    f"{item.identity} scope={item.path_scope} "
                    f"manifest={item.manifest_sha256} snapshot={item.snapshot_sha256}"
                )
        return int(StoreAutomationExit.SUCCESS)
    if action == "doctor":
        result = doctor_stores(root)
        if args.json:
            print(result.to_json())
        elif result.findings:
            for finding in result.findings:
                print(f"{finding.level:7} {finding.code}: {finding.message}")
        else:
            print(f"SpecificationStore doctor: healthy ({result.store_count} store(s))")
        return int(result.exit_code)
    if action == "context":
        result = export_store_context(
            root,
            store=args.store,
            version=args.version,
        )
        if args.json:
            print(result.to_json())
        elif not result.stores:
            print("No SpecificationStore context available")
        else:
            for item in result.stores:
                print(
                    f"{item.identity} scope={item.path_scope} "
                    f"manifest={item.manifest_sha256} snapshot={item.snapshot_sha256}"
                )
                print("  capabilities=" + ",".join(item.capabilities))
                for root_id, path in item.roots:
                    print(f"  root {root_id}={path}")
        return int(StoreAutomationExit.SUCCESS)
    raise ValueError(f"Unknown store action: {action}")


def add_store_parser(subparsers: argparse._SubParsersAction) -> None:
    store = subparsers.add_parser(
        "store",
        help="Create, register, inspect, diagnose, and export SpecificationStores",
    )
    actions = store.add_subparsers(dest="store_action", required=True)

    create = actions.add_parser("create", help="Create a managed local SpecificationStore")
    create.add_argument("store_id")
    create.add_argument("--version", required=True)
    create.add_argument("--destination", required=True)
    create.add_argument("--description", default="Local SpecificationStore")
    create.add_argument("--json", action="store_true")
    create.set_defaults(func=cmd_store)

    register = actions.add_parser("register", help="Register an existing local SpecificationStore")
    register.add_argument("store_path")
    register.add_argument("--path")
    register.add_argument("--json", action="store_true")
    register.set_defaults(func=cmd_store)

    listing = actions.add_parser("list", help="List registered SpecificationStores")
    listing.add_argument("--path")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_store)

    doctor = actions.add_parser("doctor", help="Diagnose registered SpecificationStores")
    doctor.add_argument("--path")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_store)

    context = actions.add_parser("context", help="Export deterministic SpecificationStore context")
    context.add_argument("--store")
    context.add_argument("--version")
    context.add_argument("--path")
    context.add_argument("--json", action="store_true")
    context.set_defaults(func=cmd_store)
