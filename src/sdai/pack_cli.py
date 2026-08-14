from __future__ import annotations

import argparse
import json
from pathlib import Path

from sdai.pack_catalog import load_pack_catalog, resolve_pack_catalogs
from sdai.pack_lifecycle import (
    catalog_info,
    install_from_local,
    load_install_state,
    outdated_packs,
    remove_pack,
    search_catalogs,
)
from sdai.pack_lock import load_pack_lock


def add_pack_parser(commands: argparse._SubParsersAction) -> None:
    pack = commands.add_parser("pack", help="Discover and manage deterministic SDAI Packs")
    actions = pack.add_subparsers(dest="pack_action", required=True)

    for action in ("install", "update"):
        parser = actions.add_parser(action)
        parser.add_argument("coordinate", help="Pack coordinate publisher/id")
        parser.add_argument("--lock", required=True, help="Exact Pack lock JSON")
        parser.add_argument("--source", required=True, help="Local Pack artifact directory")
        parser.add_argument("--manifest", default="pack.yaml", help="Manifest filename inside the Pack")
        parser.add_argument(
            "--local-link",
            action="store_true",
            help="Mark this development install as explicit non-production local-link provenance",
        )
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--path")

    remove = actions.add_parser("remove")
    remove.add_argument("coordinate")
    remove.add_argument("--json", action="store_true")
    remove.add_argument("--path")

    outdated = actions.add_parser("outdated")
    outdated.add_argument("--lock", required=True)
    outdated.add_argument("--json", action="store_true")
    outdated.add_argument("--path")

    info = actions.add_parser("info")
    info.add_argument("coordinate")
    info.add_argument("--catalog", action="append", required=True, help="Resolved catalog JSON; repeatable")
    info.add_argument("--json", action="store_true")
    info.add_argument("--path")

    search = actions.add_parser("search")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--catalog", action="append", required=True, help="Resolved catalog JSON; repeatable")
    search.add_argument("--json", action="store_true")
    search.add_argument("--path")


def _resolve_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else root / candidate


def _catalogs(root: Path, paths: list[str]):
    loaded = tuple(load_pack_catalog(_resolve_path(root, item)) for item in paths)
    # CLI-supplied catalogs are repository-scoped inputs. Enterprise/user scope composition
    # remains available through the framework API where scope is explicit.
    return resolve_pack_catalogs(repository=loaded)


def _emit_json(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def run_pack_command(root: Path, args: argparse.Namespace) -> int:
    action = args.pack_action

    if action in {"install", "update"}:
        lock = load_pack_lock(_resolve_path(root, args.lock))
        record = install_from_local(
            root,
            _resolve_path(root, args.source),
            lock,
            args.coordinate,
            local_link=args.local_link,
            manifest_name=args.manifest,
        )
        payload = {
            "action": action,
            "apiVersion": "sdai.pack-lifecycle-result/v1",
            "pack": record.as_dict(),
            "status": "ok",
        }
        if args.json:
            _emit_json(payload)
        else:
            provenance = " local-link" if record.mode == "local-link" else ""
            print(f"{action.capitalize()}ed Pack {record.identity}{provenance}")
            if record.preserved_paths:
                print("Preserved user-modified paths:")
                for path in record.preserved_paths:
                    print(f"  {path}")
        return 0

    if action == "remove":
        preserved = remove_pack(root, args.coordinate)
        payload = {
            "action": "remove",
            "apiVersion": "sdai.pack-lifecycle-result/v1",
            "coordinate": args.coordinate,
            "preservedPaths": list(preserved),
            "status": "ok",
        }
        if args.json:
            _emit_json(payload)
        else:
            print(f"Removed Pack {args.coordinate}")
            if preserved:
                print("Preserved user-modified paths:")
                for path in preserved:
                    print(f"  {path}")
        return 0

    if action == "outdated":
        lock = load_pack_lock(_resolve_path(root, args.lock))
        records = outdated_packs(load_install_state(root), lock)
        payload = {
            "apiVersion": "sdai.pack-outdated/v1",
            "outdated": [item.as_dict() for item in records],
        }
        if args.json:
            _emit_json(payload)
        elif not records:
            print("All installed Packs match the exact lock")
        else:
            for record in records:
                print(record.identity)
        # Stable scripting semantic: 0 means exact; 2 means actionable outdated state.
        return 2 if records else 0

    if action == "info":
        rows = catalog_info(_catalogs(root, args.catalog), args.coordinate)
        payload = {"apiVersion": "sdai.pack-info/v1", "results": list(rows)}
        if args.json:
            _emit_json(payload)
        else:
            for row in rows:
                print(f"{row['identity']} catalog={row['catalog']} source={row['source']}")
        return 0 if rows else 3

    if action == "search":
        rows = search_catalogs(_catalogs(root, args.catalog), args.query)
        payload = {"apiVersion": "sdai.pack-search/v1", "query": args.query, "results": list(rows)}
        if args.json:
            _emit_json(payload)
        else:
            for row in rows:
                print(f"{row['identity']} catalog={row['catalog']} - {row['description']}")
        return 0

    raise ValueError(f"Unknown Pack action: {action}")
