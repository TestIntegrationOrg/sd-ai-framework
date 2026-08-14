from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Mapping

from sdai.extensions.registry import RegistryLayer
from sdai.integration_manifest import IntegrationManifestError
from sdai.integration_materialization import (
    InstalledIntegration,
    IntegrationFileStatus,
    IntegrationMaterializationError,
    integration_status,
    load_install_state,
    materialize_integration,
    remove_integration,
    repair_integration,
)
from sdai.integration_registry import (
    IntegrationRegistry,
    IntegrationRegistryError,
    IntegrationSource,
    ResolvedIntegration,
    build_integration_registry,
)
from sdai.path_safety import PathSafetyError, ensure_within_project


INTEGRATION_SEARCH_API_VERSION = "sdai.integration-search/v1"
INTEGRATION_INFO_API_VERSION = "sdai.integration-info/v1"
INTEGRATION_LIFECYCLE_RESULT_API_VERSION = "sdai.integration-lifecycle-result/v1"
INTEGRATION_STATUS_COMMAND_API_VERSION = "sdai.integration-status-command/v1"
INTEGRATION_SELECTION_API_VERSION = "sdai.integration-selection/v1"
INTEGRATION_CLI_ERROR_API_VERSION = "sdai.integration-cli-error/v1"

EXIT_OK = 0
EXIT_ACTION_REQUIRED = 2
EXIT_NOT_FOUND = 3
EXIT_ERROR = 4

_SELECTION_RELATIVE = ".sdai/integrations/selection.json"


class IntegrationCliError(RuntimeError):
    pass


def _fail(code: str, message: str) -> IntegrationCliError:
    return IntegrationCliError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-INTEGRATION-CLI-001", "CLI data is not canonical finite JSON") from exc


def _emit_json(value: object) -> None:
    sys.stdout.write(_canonical_json(value) + "\n")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("SDAI-INTEGRATION-CLI-003", f"Integration selection contains duplicate JSON key '{key}'")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise _fail("SDAI-INTEGRATION-CLI-003", f"Integration selection contains non-finite JSON constant '{value}'")


def _validate_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise _fail("SDAI-INTEGRATION-CLI-003", f"{label} must be a SHA-256 digest")
    digest = value[7:]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise _fail("SDAI-INTEGRATION-CLI-003", f"{label} must be a lowercase SHA-256 digest")
    return value


def _resolve_path(root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def _add_registry_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--builtin-source", help="Override framework Integration manifest root")
    parser.add_argument("--pack-source", help="Override installed-Pack Integration manifest root")
    parser.add_argument("--org-source", help="Override authoritative organization Integration manifest root")
    parser.add_argument("--repo-source", help="Override repository Integration manifest root")
    parser.add_argument("--user-source", help="Override user Integration manifest root")


def _add_common(
    parser: argparse.ArgumentParser,
    *,
    version: bool = False,
    registry: bool = False,
) -> None:
    if version:
        parser.add_argument("--version", help="Exact Integration SemVer; omit where latest is intended")
    if registry:
        _add_registry_source_args(parser)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path")


def add_integration_parser(commands: argparse._SubParsersAction) -> None:
    integration = commands.add_parser(
        "integration",
        help="Discover and manage declarative SDAI Integrations",
    )
    actions = integration.add_subparsers(dest="integration_action", required=True)

    search = actions.add_parser("search", help="Search resolved Integrations")
    search.add_argument("query", nargs="?", default="")
    _add_common(search, registry=True)

    info = actions.add_parser("info", help="Inspect one resolved Integration")
    info.add_argument("integration_id")
    _add_common(info, version=True, registry=True)

    install = actions.add_parser(
        "install",
        help="Materialize an Integration without silently changing versions",
    )
    install.add_argument("integration_id")
    _add_common(install, version=True, registry=True)

    use = actions.add_parser("use", help="Select an already installed Integration")
    use.add_argument("integration_id")
    _add_common(use, version=True)

    status = actions.add_parser(
        "status",
        help="Compare installed state with the desired resolved Integration",
    )
    status.add_argument("integration_id")
    _add_common(status, version=True, registry=True)

    repair = actions.add_parser(
        "repair",
        help="Repair the installed exact Integration version",
    )
    repair.add_argument("integration_id")
    _add_common(repair, version=True, registry=True)

    upgrade = actions.add_parser(
        "upgrade",
        help="Upgrade an installed Integration to latest or an exact version",
    )
    upgrade.add_argument("integration_id")
    _add_common(upgrade, version=True, registry=True)

    remove = actions.add_parser(
        "remove",
        help="Remove managed files while preserving user-modified content",
    )
    remove.add_argument("integration_id")
    _add_common(remove)


def _source_override(args: argparse.Namespace, name: str) -> str | None:
    value = getattr(args, f"{name}_source", None)
    return value if isinstance(value, str) and value else None


def _optional_source(
    root: Path,
    *,
    layer: RegistryLayer,
    source: str,
    explicit: str | None,
    default: Path | None,
    locked: bool = False,
) -> IntegrationSource | None:
    if explicit is not None:
        path = _resolve_path(root, explicit)
        if not path.exists() or not path.is_dir():
            raise _fail(
                "SDAI-INTEGRATION-CLI-002",
                f"explicit {layer.value} Integration source '{path}' must be an existing directory",
            )
        return IntegrationSource(path, layer, source, locked=locked)
    if default is None or not default.exists():
        return None
    if not default.is_dir():
        raise _fail(
            "SDAI-INTEGRATION-CLI-002",
            f"default {layer.value} Integration source '{default}' is not a directory",
        )
    return IntegrationSource(default, layer, source, locked=locked)


def _registry_sources(root: Path, args: argparse.Namespace) -> tuple[IntegrationSource, ...]:
    package_builtin = Path(__file__).resolve().parent / "builtin_integrations"
    pack_default = root / ".sdai" / "installed-packs"
    repo_default = root / ".sdai" / "integrations" / "manifests"

    org_env = os.environ.get("SDAI_ORG_INTEGRATIONS_PATH")
    org_default = _resolve_path(root, org_env) if org_env else None

    user_env = os.environ.get("SDAI_USER_INTEGRATIONS_PATH")
    user_default = (
        _resolve_path(root, user_env)
        if user_env
        else Path.home() / ".sdai" / "integrations"
    )

    candidates = (
        # Built-ins are lowest precedence by default. Locking is an explicit registry
        # policy choice; the CLI must not turn every framework default into an
        # un-overridable definition merely because it was packaged with SDAI.
        _optional_source(
            root,
            layer=RegistryLayer.BUILTIN,
            source="framework",
            explicit=_source_override(args, "builtin"),
            default=package_builtin,
            locked=False,
        ),
        _optional_source(
            root,
            layer=RegistryLayer.PACK,
            source="installed-packs",
            explicit=_source_override(args, "pack"),
            default=pack_default,
        ),
        _optional_source(
            root,
            layer=RegistryLayer.ORG,
            source="organization",
            explicit=_source_override(args, "org"),
            default=org_default,
            locked=True,
        ),
        _optional_source(
            root,
            layer=RegistryLayer.REPO,
            source="repository",
            explicit=_source_override(args, "repo"),
            default=repo_default,
        ),
        _optional_source(
            root,
            layer=RegistryLayer.USER,
            source="user",
            explicit=_source_override(args, "user"),
            default=user_default,
        ),
    )
    return tuple(item for item in candidates if item is not None)


def build_cli_integration_registry(
    root: Path,
    args: argparse.Namespace,
) -> IntegrationRegistry:
    return build_integration_registry(_registry_sources(root, args))


def _installed(root: Path, integration_id: str) -> InstalledIntegration | None:
    state = load_install_state(root)
    return next(
        (item for item in state.integrations if item.id == integration_id),
        None,
    )


def _selection_path(root: Path) -> Path:
    root = root.resolve()
    candidate = root / _SELECTION_RELATIVE
    try:
        ensure_within_project(root, candidate, label="Integration selection")
    except PathSafetyError as exc:
        raise _fail(
            "SDAI-INTEGRATION-CLI-003",
            "Integration selection escapes the project root",
        ) from exc
    current = root
    for part in Path(_SELECTION_RELATIVE).parts:
        current = current / part
        if current.is_symlink():
            raise _fail(
                "SDAI-INTEGRATION-CLI-003",
                "Integration selection path must not contain symlinks",
            )
    return candidate


@dataclass(frozen=True)
class IntegrationSelection:
    id: str
    identity: str
    version: str
    manifest_sha256: str
    provenance_layer: str
    provenance_source: str
    provenance_path: str

    @classmethod
    def from_installed(
        cls,
        record: InstalledIntegration,
    ) -> "IntegrationSelection":
        return cls(
            id=record.id,
            identity=record.identity,
            version=record.version,
            manifest_sha256=record.manifest_sha256,
            provenance_layer=record.provenance_layer,
            provenance_source=record.provenance_source,
            provenance_path=record.provenance_path,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "identity": self.identity,
            "manifestSha256": self.manifest_sha256,
            "provenance": {
                "layer": self.provenance_layer,
                "path": self.provenance_path,
                "source": self.provenance_source,
            },
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: object) -> "IntegrationSelection":
        expected = {"id", "identity", "manifestSha256", "provenance", "version"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise _fail(
                "SDAI-INTEGRATION-CLI-003",
                "Integration selection record is invalid",
            )
        provenance = value["provenance"]
        if not isinstance(provenance, Mapping) or set(provenance) != {
            "layer",
            "path",
            "source",
        }:
            raise _fail(
                "SDAI-INTEGRATION-CLI-003",
                "Integration selection provenance is invalid",
            )
        values = (
            value["id"],
            value["identity"],
            value["version"],
            provenance["layer"],
            provenance["path"],
            provenance["source"],
        )
        if not all(isinstance(item, str) and item for item in values):
            raise _fail(
                "SDAI-INTEGRATION-CLI-003",
                "Integration selection fields must be non-empty strings",
            )
        if value["identity"] != f"{value['id']}@{value['version']}":
            raise _fail(
                "SDAI-INTEGRATION-CLI-003",
                "Integration selection identity/version mismatch",
            )
        return cls(
            id=value["id"],
            identity=value["identity"],
            version=value["version"],
            manifest_sha256=_validate_sha256(
                value["manifestSha256"],
                label="Integration selection manifestSha256",
            ),
            provenance_layer=provenance["layer"],
            provenance_source=provenance["source"],
            provenance_path=provenance["path"],
        )


def _load_selection(root: Path) -> IntegrationSelection | None:
    path = _selection_path(root)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise _fail(
            "SDAI-INTEGRATION-CLI-003",
            "Integration selection must be a regular file",
        )
    try:
        value = json.loads(
            path.read_text(encoding="utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail(
            "SDAI-INTEGRATION-CLI-003",
            "Integration selection must be valid UTF-8 JSON",
        ) from exc
    if not isinstance(value, Mapping) or set(value) != {"apiVersion", "selection"}:
        raise _fail(
            "SDAI-INTEGRATION-CLI-003",
            "Integration selection contract is invalid",
        )
    if value["apiVersion"] != INTEGRATION_SELECTION_API_VERSION:
        raise _fail(
            "SDAI-INTEGRATION-CLI-003",
            "Integration selection apiVersion is unsupported",
        )
    return IntegrationSelection.from_dict(value["selection"])


def _write_selection(root: Path, selection: IntegrationSelection) -> None:
    path = _selection_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise _fail(
            "SDAI-INTEGRATION-CLI-003",
            "Integration selection path must not be a symlink",
        )
    payload = {
        "apiVersion": INTEGRATION_SELECTION_API_VERSION,
        "selection": selection.as_dict(),
    }
    data = (_canonical_json(payload) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".selection.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise _fail(
            "SDAI-INTEGRATION-CLI-003",
            "unable to atomically persist Integration selection",
        ) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _clear_selection(root: Path) -> None:
    try:
        _selection_path(root).unlink(missing_ok=True)
    except OSError as exc:
        raise _fail(
            "SDAI-INTEGRATION-CLI-003",
            "unable to clear Integration selection",
        ) from exc


def _selected(
    root: Path,
    integration_id: str,
    identity: str | None = None,
) -> bool:
    selection = _load_selection(root)
    return bool(
        selection is not None
        and selection.id == integration_id
        and (identity is None or selection.identity == identity)
    )


def _result_payload(
    action: str,
    status: str,
    integration_id: str,
    *,
    record: InstalledIntegration | None = None,
    desired: ResolvedIntegration | None = None,
    preserved: tuple[str, ...] = (),
    selection_cleared: bool = False,
) -> dict[str, object]:
    return {
        "action": action,
        "apiVersion": INTEGRATION_LIFECYCLE_RESULT_API_VERSION,
        "desired": None if desired is None else desired.as_dict(),
        "installed": None if record is None else record.as_dict(),
        "integrationId": integration_id,
        "preservedPaths": list(preserved),
        "selectionCleared": selection_cleared,
        "status": status,
    }


def _emit_result(
    args: argparse.Namespace,
    payload: dict[str, object],
    *,
    human: str,
) -> None:
    if args.json:
        _emit_json(payload)
    else:
        print(human)


def _not_found(
    args: argparse.Namespace,
    action: str,
    integration_id: str,
    *,
    detail: str,
) -> int:
    payload = _result_payload(action, "not-found", integration_id)
    if args.json:
        _emit_json(payload)
    else:
        print(f"Integration {integration_id}: {detail}")
    return EXIT_NOT_FOUND


def _resolution(
    registry: IntegrationRegistry,
    integration_id: str,
    version: str | None,
) -> ResolvedIntegration | None:
    return registry.resolve(integration_id, version)


def _search_row(root: Path, resolved: ResolvedIntegration) -> dict[str, object]:
    installed = _installed(root, resolved.id)
    return {
        "installed": None if installed is None else installed.as_dict(),
        "resolution": resolved.as_dict(),
        "selected": _selected(root, resolved.id, resolved.identity),
    }


def _run_search(root: Path, args: argparse.Namespace) -> int:
    registry = build_cli_integration_registry(root, args)
    rows = [_search_row(root, item) for item in registry.search(args.query)]
    payload = {
        "apiVersion": INTEGRATION_SEARCH_API_VERSION,
        "query": args.query,
        "registrySha256": registry.sha256,
        "results": rows,
    }
    if args.json:
        _emit_json(payload)
    else:
        for row in rows:
            resolution = row["resolution"]
            assert isinstance(resolution, dict)
            manifest = resolution["manifest"]
            assert isinstance(manifest, dict)
            suffix = " [selected]" if row["selected"] else ""
            print(f"{resolution['identity']}{suffix} - {manifest['description']}")
    return EXIT_OK


def _run_info(root: Path, args: argparse.Namespace) -> int:
    registry = build_cli_integration_registry(root, args)
    resolved = _resolution(registry, args.integration_id, args.version)
    installed = _installed(root, args.integration_id)
    payload = {
        "apiVersion": INTEGRATION_INFO_API_VERSION,
        "installed": None if installed is None else installed.as_dict(),
        "integrationId": args.integration_id,
        "registrySha256": registry.sha256,
        "resolution": None if resolved is None else resolved.as_dict(),
        "selected": _selected(
            root,
            args.integration_id,
            None if resolved is None else resolved.identity,
        ),
    }
    if args.json:
        _emit_json(payload)
    elif resolved is not None:
        provenance = resolved.selected_provenance
        print(
            f"{resolved.identity} layer={provenance.layer.value} "
            f"source={provenance.source}/{provenance.path}"
        )
    else:
        print(f"Integration {args.integration_id} was not found")
    return EXIT_OK if resolved is not None else EXIT_NOT_FOUND


def _run_install(root: Path, args: argparse.Namespace) -> int:
    registry = build_cli_integration_registry(root, args)
    desired = _resolution(registry, args.integration_id, args.version)
    if desired is None:
        return _not_found(
            args,
            "install",
            args.integration_id,
            detail="requested version is not available",
        )
    existing = _installed(root, args.integration_id)
    if existing is not None and existing.identity != desired.identity:
        payload = _result_payload(
            "install",
            "different-version-installed",
            args.integration_id,
            record=existing,
            desired=desired,
        )
        _emit_result(
            args,
            payload,
            human=(
                f"Integration {existing.identity} is already installed; use "
                f"`sdai integration upgrade` for {desired.identity}"
            ),
        )
        return EXIT_ACTION_REQUIRED
    record = materialize_integration(root, desired)
    payload = _result_payload(
        "install",
        "ok",
        args.integration_id,
        record=record,
        desired=desired,
    )
    _emit_result(args, payload, human=f"Installed Integration {record.identity}")
    return EXIT_OK


def _run_use(root: Path, args: argparse.Namespace) -> int:
    record = _installed(root, args.integration_id)
    if record is None:
        return _not_found(
            args,
            "use",
            args.integration_id,
            detail="it is not installed",
        )
    if args.version is not None and record.version != args.version:
        payload = _result_payload(
            "use",
            "different-version-installed",
            args.integration_id,
            record=record,
        )
        _emit_result(
            args,
            payload,
            human=(
                f"Installed Integration is {record.identity}, not "
                f"{args.integration_id}@{args.version}"
            ),
        )
        return EXIT_ACTION_REQUIRED
    selection = IntegrationSelection.from_installed(record)
    _write_selection(root, selection)
    payload = {
        "action": "use",
        "apiVersion": INTEGRATION_LIFECYCLE_RESULT_API_VERSION,
        "integrationId": args.integration_id,
        "selection": selection.as_dict(),
        "status": "ok",
    }
    _emit_result(args, payload, human=f"Selected Integration {record.identity}")
    return EXIT_OK


def _run_status(root: Path, args: argparse.Namespace) -> int:
    registry = build_cli_integration_registry(root, args)
    installed = _installed(root, args.integration_id)
    desired = _resolution(registry, args.integration_id, args.version)

    if desired is None:
        if installed is None:
            payload = {
                "apiVersion": INTEGRATION_STATUS_COMMAND_API_VERSION,
                "installed": None,
                "integrationId": args.integration_id,
                "registryStatus": "not-found",
                "report": None,
                "selected": _selected(root, args.integration_id),
                "status": "not-found",
            }
            if args.json:
                _emit_json(payload)
            else:
                print(
                    f"Integration {args.integration_id} is neither installed nor available"
                )
            return EXIT_NOT_FOUND

        payload = {
            "apiVersion": INTEGRATION_STATUS_COMMAND_API_VERSION,
            "installed": installed.as_dict(),
            "integrationId": args.integration_id,
            "registryStatus": "not-found",
            "report": None,
            "selected": _selected(root, args.integration_id, installed.identity),
            "status": "orphaned",
        }
        if args.json:
            _emit_json(payload)
        else:
            print(
                f"Integration {installed.identity} is installed but no matching "
                "registry target is available"
            )
        return EXIT_ACTION_REQUIRED

    report = integration_status(root, desired)
    payload = {
        "apiVersion": INTEGRATION_STATUS_COMMAND_API_VERSION,
        "installed": None if installed is None else installed.as_dict(),
        "integrationId": args.integration_id,
        "registryStatus": "resolved",
        "report": report.as_dict(),
        "selected": _selected(
            root,
            args.integration_id,
            None if installed is None else installed.identity,
        ),
        "status": report.status.value,
    }
    if args.json:
        _emit_json(payload)
    else:
        print(
            f"Integration {args.integration_id} status={report.status.value} "
            f"desired={desired.identity}"
        )
        for finding in report.findings:
            path = finding.path or "<integration>"
            print(
                f"  {finding.status.value:18} {path} - {finding.detail}"
            )
    return (
        EXIT_OK
        if report.status == IntegrationFileStatus.EXACT
        else EXIT_ACTION_REQUIRED
    )


def _run_repair(root: Path, args: argparse.Namespace) -> int:
    record = _installed(root, args.integration_id)
    if record is None:
        return _not_found(
            args,
            "repair",
            args.integration_id,
            detail="it is not installed",
        )
    if args.version is not None and record.version != args.version:
        payload = _result_payload(
            "repair",
            "different-version-installed",
            args.integration_id,
            record=record,
        )
        _emit_result(
            args,
            payload,
            human=(
                f"Repair targets installed {record.identity}; use upgrade to change versions"
            ),
        )
        return EXIT_ACTION_REQUIRED

    registry = build_cli_integration_registry(root, args)
    desired = registry.resolve(args.integration_id, record.version)
    if desired is None:
        return _not_found(
            args,
            "repair",
            args.integration_id,
            detail=f"installed exact version {record.version} is unavailable",
        )
    repaired = repair_integration(root, desired)
    payload = _result_payload(
        "repair",
        "ok",
        args.integration_id,
        record=repaired,
        desired=desired,
    )
    _emit_result(args, payload, human=f"Repaired Integration {repaired.identity}")
    return EXIT_OK


def _run_upgrade(root: Path, args: argparse.Namespace) -> int:
    existing = _installed(root, args.integration_id)
    if existing is None:
        return _not_found(
            args,
            "upgrade",
            args.integration_id,
            detail="it is not installed; use install first",
        )
    registry = build_cli_integration_registry(root, args)
    desired = _resolution(registry, args.integration_id, args.version)
    if desired is None:
        return _not_found(
            args,
            "upgrade",
            args.integration_id,
            detail="requested target version is unavailable",
        )
    upgraded = materialize_integration(root, desired)
    payload = _result_payload(
        "upgrade",
        "ok",
        args.integration_id,
        record=upgraded,
        desired=desired,
    )
    _emit_result(
        args,
        payload,
        human=f"Upgraded Integration {existing.identity} -> {upgraded.identity}",
    )
    return EXIT_OK


def _run_remove(root: Path, args: argparse.Namespace) -> int:
    # Validate selection state before native-file mutation so malformed advisory state
    # cannot surprise the caller only after a successful destructive remove.
    selection = _load_selection(root)
    before = _installed(root, args.integration_id)
    preserved = remove_integration(root, args.integration_id)
    selection_cleared = selection is not None and selection.id == args.integration_id
    if selection_cleared:
        _clear_selection(root)
    payload = _result_payload(
        "remove",
        "ok",
        args.integration_id,
        record=before,
        preserved=preserved,
        selection_cleared=selection_cleared,
    )
    _emit_result(args, payload, human=f"Removed Integration {args.integration_id}")
    if not args.json and preserved:
        print("Preserved user-modified paths:")
        for path in preserved:
            print(f"  {path}")
    return EXIT_OK


def _error_code(exc: BaseException) -> str:
    prefix = str(exc).split(":", 1)[0]
    return (
        prefix
        if prefix.startswith("SDAI-")
        else "SDAI-INTEGRATION-CLI-999"
    )


def _emit_error(args: argparse.Namespace, exc: BaseException) -> int:
    payload = {
        "action": getattr(args, "integration_action", "unknown"),
        "apiVersion": INTEGRATION_CLI_ERROR_API_VERSION,
        "code": _error_code(exc),
        "message": str(exc),
        "status": "error",
    }
    if getattr(args, "json", False):
        _emit_json(payload)
    else:
        print(f"error: {exc}", file=sys.stderr)
    return EXIT_ERROR


def run_integration_command(root: Path, args: argparse.Namespace) -> int:
    try:
        action = args.integration_action
        if action == "search":
            return _run_search(root, args)
        if action == "info":
            return _run_info(root, args)
        if action == "install":
            return _run_install(root, args)
        if action == "use":
            return _run_use(root, args)
        if action == "status":
            return _run_status(root, args)
        if action == "repair":
            return _run_repair(root, args)
        if action == "upgrade":
            return _run_upgrade(root, args)
        if action == "remove":
            return _run_remove(root, args)
        raise _fail(
            "SDAI-INTEGRATION-CLI-001",
            f"unknown Integration action '{action}'",
        )
    except (
        IntegrationCliError,
        IntegrationManifestError,
        IntegrationMaterializationError,
        IntegrationRegistryError,
        FileNotFoundError,
        FileExistsError,
        OSError,
        UnicodeError,
        ValueError,
    ) as exc:
        return _emit_error(args, exc)
