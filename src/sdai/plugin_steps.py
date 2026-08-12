from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Mapping, Protocol

import yaml

from sdai.extensions.manifests import ExtensionKind, ExtensionManifestError, parse_extension_manifest
from sdai.path_safety import ensure_within_project
from sdai.text import TextEncodingError, read_utf8_text


class PluginStepError(RuntimeError):
    """Raised when a plugin manifest, policy, permission, or execution result is unsafe."""


_SAFE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_EXECUTABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_FORBIDDEN_TEMPLATE = re.compile(r"\$\{\{|\}\}")
_WINDOWS_INVALID_CHARS = frozenset('<>:"|?*')
_DOS_DEVICE = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PluginPermissions:
    filesystem_read: tuple[str, ...] = ()
    filesystem_write: tuple[str, ...] = ()
    network: bool = False
    environment: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    workspace_write: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "filesystem": {
                "read": list(self.filesystem_read),
                "write": list(self.filesystem_write),
            },
            "network": self.network,
            "environment": list(self.environment),
            "commands": list(self.commands),
            "workspace_write": self.workspace_write,
        }


@dataclass(frozen=True)
class PluginManifest:
    id: str
    version: str
    publisher: str
    executor: str
    permissions: PluginPermissions
    source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "version": self.version,
            "publisher": self.publisher,
            "executor": self.executor,
            "permissions": self.permissions.as_dict(),
            "source": self.source,
        }


@dataclass(frozen=True)
class PluginPolicy:
    allowed_plugins: tuple[str, ...] | None
    denied_plugins: tuple[str, ...]
    trusted_publishers: tuple[str, ...]
    allow_workspace_write: bool
    allow_network: bool
    allowed_read_paths: tuple[str, ...]
    allowed_write_paths: tuple[str, ...]
    allowed_environment: tuple[str, ...]
    allowed_commands: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class PluginExecutionPlan:
    plugin: PluginManifest
    step_id: str
    inputs: dict[str, object]
    permissions: PluginPermissions
    policy_sources: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "plugin": self.plugin.as_dict(),
            "step_id": self.step_id,
            "inputs": self.inputs,
            "effective_permissions": self.permissions.as_dict(),
            "policy_sources": list(self.policy_sources),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class PluginFinding:
    code: str
    severity: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True)
class PluginResult:
    status: str
    summary: str
    findings: tuple[PluginFinding, ...] = ()
    data: Mapping[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "status": self.status,
            "summary": self.summary,
            "findings": [item.as_dict() for item in self.findings],
            "data": dict(self.data or {}),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)


class PluginExecutor(Protocol):
    def execute(
        self,
        plan: PluginExecutionPlan,
        services: "PluginExecutionServices",
    ) -> PluginResult: ...


class PluginExecutorRegistry:
    """Registry of executor implementations installed/trusted by SDAI code.

    Manifests name an executor ID only. They cannot import a Python module or
    specify an arbitrary callable. Installing executable plugin code is therefore
    a separate trusted-publisher/package operation rather than a YAML capability.
    """

    def __init__(self) -> None:
        self._executors: dict[str, PluginExecutor] = {}

    def register(self, executor_id: str, executor: PluginExecutor) -> None:
        executor_id = _safe_id(executor_id, "executor id")
        if executor_id in self._executors:
            raise _fail(
                "SDAI-PLUGIN-006",
                f"executor '{executor_id}' is already registered",
            )
        self._executors[executor_id] = executor

    def get(self, executor_id: str) -> PluginExecutor | None:
        return self._executors.get(executor_id)

    def clear(self) -> None:
        self._executors.clear()


EXECUTORS = PluginExecutorRegistry()


def _fail(code: str, message: str) -> PluginStepError:
    return PluginStepError(f"{code}: {message}")


def _safe_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not _SAFE_ID.fullmatch(value.strip())
        or ".." in value
    ):
        raise _fail("SDAI-PLUGIN-001", f"{label} is invalid")
    return value.strip()


def _safe_repo_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(
            "SDAI-PLUGIN-001",
            f"{label} must be a non-empty repository-relative path",
        )
    text = value.strip()
    if text == ".":
        return "."
    if (
        "\\" in text
        or text.startswith("/")
        or re.match(r"^[A-Za-z]:", text)
    ):
        raise _fail(
            "SDAI-PLUGIN-001",
            f"{label} must be a repository-relative POSIX path",
        )
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _fail(
            "SDAI-PLUGIN-001",
            f"{label} contains an invalid path segment",
        )
    for part in parts:
        if (
            any(char in _WINDOWS_INVALID_CHARS for char in part)
            or part.endswith((".", " "))
            or any(ord(char) < 32 for char in part)
            or _DOS_DEVICE.fullmatch(part)
        ):
            raise _fail(
                "SDAI-PLUGIN-001",
                f"{label} is not portable across Windows/Linux",
            )
    return text


def _string_list(
    value: object,
    label: str,
    validator=None,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise _fail("SDAI-PLUGIN-001", f"{label} must be a string list")
    result = tuple(
        validator(item, label) if validator else item.strip()
        for item in value
    )
    if len(result) != len(set(result)):
        raise _fail("SDAI-PLUGIN-001", f"{label} must not contain duplicates")
    return result


def _parse_permissions(raw: object, label: str) -> PluginPermissions:
    if not isinstance(raw, Mapping):
        raise _fail(
            "SDAI-PLUGIN-001",
            f"{label} permissions must be a mapping",
        )
    allowed = {
        "filesystem",
        "network",
        "environment",
        "commands",
        "workspace_write",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise _fail(
            "SDAI-PLUGIN-001",
            f"{label} permissions has unknown field(s): "
            + ", ".join(map(str, unknown)),
        )
    filesystem = raw.get("filesystem") or {}
    if (
        not isinstance(filesystem, Mapping)
        or set(filesystem) - {"read", "write"}
    ):
        raise _fail(
            "SDAI-PLUGIN-001",
            f"{label} permissions.filesystem must contain only read/write",
        )
    network = raw.get("network", False)
    workspace_write = raw.get("workspace_write", False)
    if not isinstance(network, bool) or not isinstance(workspace_write, bool):
        raise _fail(
            "SDAI-PLUGIN-001",
            f"{label} network/workspace_write must be true or false",
        )
    environment = _string_list(raw.get("environment"), f"{label} environment")
    if any(not _ENV_NAME.fullmatch(name) for name in environment):
        raise _fail(
            "SDAI-PLUGIN-001",
            f"{label} environment contains invalid variable name",
        )
    commands = _string_list(raw.get("commands"), f"{label} commands")
    if any(not _EXECUTABLE.fullmatch(item) for item in commands):
        raise _fail(
            "SDAI-PLUGIN-001",
            f"{label} commands must be bare executable names",
        )
    return PluginPermissions(
        filesystem_read=_string_list(
            filesystem.get("read"),
            f"{label} filesystem.read",
            _safe_repo_path,
        ),
        filesystem_write=_string_list(
            filesystem.get("write"),
            f"{label} filesystem.write",
            _safe_repo_path,
        ),
        network=network,
        environment=environment,
        commands=commands,
        workspace_write=workspace_write,
    )


def _plugin_path(root: Path, plugin_id: str) -> Path:
    candidates = (
        root / ".sdai" / "plugin-steps" / f"{plugin_id}.yaml",
        root / ".sdai" / "extensions" / "plugin-steps" / f"{plugin_id}.yaml",
    )
    existing = [
        ensure_within_project(root, path, label="plugin step manifest")
        for path in candidates
        if path.exists()
    ]
    if len(existing) > 1:
        raise _fail(
            "SDAI-PLUGIN-001",
            f"plugin '{plugin_id}' exists in more than one location",
        )
    if not existing:
        raise _fail(
            "SDAI-PLUGIN-001",
            f"plugin '{plugin_id}' does not exist",
        )
    path = existing[0]
    if path.is_symlink() or not path.is_file():
        raise _fail(
            "SDAI-PLUGIN-001",
            f"plugin '{plugin_id}' must be a regular non-symlink file",
        )
    return path


def load_plugin_manifest(
    project_root: Path,
    plugin_id: str,
) -> PluginManifest:
    root = project_root.resolve()
    plugin_id = _safe_id(plugin_id, "plugin id")
    path = _plugin_path(root, plugin_id)
    source = path.relative_to(root).as_posix()
    try:
        raw = yaml.safe_load(read_utf8_text(path)) or {}
    except (OSError, TextEncodingError, yaml.YAMLError) as exc:
        raise _fail(
            "SDAI-PLUGIN-001",
            f"unable to read plugin '{plugin_id}': {exc}",
        ) from exc
    if not isinstance(raw, Mapping):
        raise _fail(
            "SDAI-PLUGIN-001",
            f"plugin '{plugin_id}' must be a YAML mapping",
        )
    try:
        manifest = parse_extension_manifest(raw, source=source)
    except ExtensionManifestError as exc:
        raise _fail(
            "SDAI-PLUGIN-001",
            f"invalid plugin '{plugin_id}': {exc}",
        ) from exc
    if manifest.kind is not ExtensionKind.PLUGIN_STEP:
        raise _fail(
            "SDAI-PLUGIN-001",
            f"plugin '{plugin_id}' kind must be PluginStep",
        )
    if manifest.metadata.id != plugin_id:
        raise _fail(
            "SDAI-PLUGIN-001",
            f"plugin filename/id mismatch for '{plugin_id}'",
        )
    unknown = sorted(
        set(manifest.spec) - {"publisher", "executor", "permissions"}
    )
    if unknown:
        raise _fail(
            "SDAI-PLUGIN-001",
            f"plugin '{plugin_id}' has unknown spec field(s): "
            + ", ".join(unknown),
        )
    publisher = _safe_id(manifest.spec.get("publisher"), "publisher")
    executor = _safe_id(manifest.spec.get("executor"), "executor")
    permissions = _parse_permissions(
        manifest.spec.get("permissions"),
        f"plugin '{plugin_id}'",
    )
    if permissions.network:
        raise _fail(
            "SDAI-PLUGIN-004",
            "network permission is not supported by the cross-platform v1 execution boundary",
        )
    return PluginManifest(
        plugin_id,
        manifest.metadata.version,
        publisher,
        executor,
        permissions,
        source,
    )


_POLICY_KEYS = frozenset(
    {
        "version",
        "allowed_plugins",
        "denied_plugins",
        "trusted_publishers",
        "permissions",
    }
)


def _read_policy(path: Path, source: str) -> dict[str, object]:
    try:
        raw = yaml.safe_load(read_utf8_text(path)) or {}
    except (OSError, TextEncodingError, yaml.YAMLError) as exc:
        raise _fail(
            "SDAI-PLUGIN-002",
            f"unable to read plugin policy {source}: {exc}",
        ) from exc
    if not isinstance(raw, Mapping):
        raise _fail(
            "SDAI-PLUGIN-002",
            f"plugin policy {source} must be a mapping",
        )
    unknown = sorted(set(raw) - _POLICY_KEYS)
    if unknown or raw.get("version") != 1:
        raise _fail(
            "SDAI-PLUGIN-002",
            f"plugin policy {source} must use version 1 and supported fields only",
        )
    return dict(raw)


def _policy_path(value: str | None, label: str) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise _fail(
            "SDAI-PLUGIN-002",
            f"{label} must be an absolute regular non-symlink file",
        )
    return path


def _policy_permissions(raw: object, source: str) -> PluginPermissions:
    if raw is None:
        return PluginPermissions()
    return _parse_permissions(raw, f"policy {source}")


def _intersection(
    current: set[str] | None,
    incoming: tuple[str, ...] | None,
) -> set[str] | None:
    if incoming is None:
        return current
    values = set(incoming)
    return values if current is None else current & values


def load_plugin_policy(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> PluginPolicy:
    root = project_root.resolve()
    env = dict(os.environ if environ is None else environ)
    sources: list[tuple[Path, str]] = []

    org = _policy_path(
        env.get("SDAI_ORG_PLUGIN_POLICY_PATH"),
        "SDAI_ORG_PLUGIN_POLICY_PATH",
    )
    if org:
        sources.append((org, org.resolve().as_posix()))

    repo = root / ".sdai" / "plugin-policy.yaml"
    if repo.exists():
        safe = ensure_within_project(
            root,
            repo,
            label="repository plugin policy",
        )
        if safe.is_symlink() or not safe.is_file():
            raise _fail(
                "SDAI-PLUGIN-002",
                ".sdai/plugin-policy.yaml must be a regular non-symlink file",
            )
        sources.append((safe, ".sdai/plugin-policy.yaml"))

    user = _policy_path(
        env.get("SDAI_USER_PLUGIN_POLICY_PATH"),
        "SDAI_USER_PLUGIN_POLICY_PATH",
    )
    if user:
        sources.append((user, user.resolve().as_posix()))

    allowed_plugins: set[str] | None = None
    denied_plugins: set[str] = set()
    trusted_publishers: set[str] | None = None
    read_paths: set[str] | None = None
    write_paths: set[str] | None = None
    environment_names: set[str] | None = None
    commands: set[str] | None = None
    allow_workspace_write: bool | None = None
    allow_network: bool | None = None
    provenance: list[str] = []

    for path, source in sources:
        raw = _read_policy(path, source)
        provenance.append(source)
        if "allowed_plugins" in raw:
            allowed_plugins = _intersection(
                allowed_plugins,
                _string_list(
                    raw.get("allowed_plugins"),
                    f"policy {source} allowed_plugins",
                    _safe_id,
                ),
            )
        denied_plugins.update(
            _string_list(
                raw.get("denied_plugins"),
                f"policy {source} denied_plugins",
                _safe_id,
            )
        )
        if "trusted_publishers" in raw:
            trusted_publishers = _intersection(
                trusted_publishers,
                _string_list(
                    raw.get("trusted_publishers"),
                    f"policy {source} trusted_publishers",
                    _safe_id,
                ),
            )
        permissions = raw.get("permissions")
        if permissions is not None:
            parsed = _policy_permissions(permissions, source)
            read_paths = _intersection(read_paths, parsed.filesystem_read)
            write_paths = _intersection(write_paths, parsed.filesystem_write)
            environment_names = _intersection(
                environment_names,
                parsed.environment,
            )
            commands = _intersection(commands, parsed.commands)
            allow_workspace_write = (
                parsed.workspace_write
                if allow_workspace_write is None
                else allow_workspace_write and parsed.workspace_write
            )
            allow_network = (
                parsed.network
                if allow_network is None
                else allow_network and parsed.network
            )

    if not sources:
        trusted_publishers = {"sdai"}

    return PluginPolicy(
        allowed_plugins=(
            None
            if allowed_plugins is None
            else tuple(sorted(allowed_plugins))
        ),
        denied_plugins=tuple(sorted(denied_plugins)),
        trusted_publishers=tuple(sorted(trusted_publishers or ())),
        allow_workspace_write=bool(allow_workspace_write),
        allow_network=bool(allow_network),
        allowed_read_paths=tuple(sorted(read_paths or ())),
        allowed_write_paths=tuple(sorted(write_paths or ())),
        allowed_environment=tuple(sorted(environment_names or ())),
        allowed_commands=tuple(sorted(commands or ())),
        sources=tuple(provenance),
    )


def _path_allowed(requested: str, allowed: tuple[str, ...]) -> bool:
    if requested == ".":
        return "." in allowed
    return any(
        prefix == "."
        or requested == prefix
        or requested.startswith(prefix.rstrip("/") + "/")
        for prefix in allowed
    )


def _validate_json_inputs(value: object, *, label: str) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if "\x00" in value or _FORBIDDEN_TEMPLATE.search(value):
            raise _fail(
                "SDAI-PLUGIN-001",
                f"{label} contains NUL or unsupported template syntax",
            )
        return value
    if isinstance(value, list):
        return [
            _validate_json_inputs(item, label=label)
            for item in value
        ]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise _fail(
                    "SDAI-PLUGIN-001",
                    f"{label} mapping keys must be non-empty strings",
                )
            result[key] = _validate_json_inputs(item, label=label)
        return result
    raise _fail(
        "SDAI-PLUGIN-001",
        f"{label} must contain JSON-compatible values",
    )


def prepare_plugin_step(
    project_root: Path,
    plugin_id: str,
    step_id: str,
    inputs: Mapping[str, object] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> PluginExecutionPlan:
    root = project_root.resolve()
    plugin = load_plugin_manifest(root, plugin_id)
    policy = load_plugin_policy(root, environ=environ)

    if plugin.id in policy.denied_plugins:
        raise _fail(
            "SDAI-PLUGIN-003",
            f"plugin '{plugin.id}' is denied by effective policy",
        )
    if (
        policy.allowed_plugins is not None
        and plugin.id not in policy.allowed_plugins
    ):
        raise _fail(
            "SDAI-PLUGIN-003",
            f"plugin '{plugin.id}' is not in the effective allowed_plugins set",
        )
    if plugin.publisher not in policy.trusted_publishers:
        raise _fail(
            "SDAI-PLUGIN-003",
            f"plugin publisher '{plugin.publisher}' is not trusted by effective policy",
        )

    requested = plugin.permissions
    if requested.workspace_write and not policy.allow_workspace_write:
        raise _fail(
            "SDAI-PLUGIN-003",
            "workspace_write permission is denied by effective policy",
        )
    if requested.network:
        raise _fail(
            "SDAI-PLUGIN-004",
            "network permission is not supported in plugin v1",
        )
    for path in requested.filesystem_read:
        if not _path_allowed(path, policy.allowed_read_paths):
            raise _fail(
                "SDAI-PLUGIN-003",
                f"filesystem read permission '{path}' is denied by effective policy",
            )
    for path in requested.filesystem_write:
        if not _path_allowed(path, policy.allowed_write_paths):
            raise _fail(
                "SDAI-PLUGIN-003",
                f"filesystem write permission '{path}' is denied by effective policy",
            )
    missing_environment = sorted(
        set(requested.environment) - set(policy.allowed_environment)
    )
    if missing_environment:
        raise _fail(
            "SDAI-PLUGIN-003",
            "environment permission denied: "
            + ", ".join(missing_environment),
        )
    missing_commands = sorted(
        set(requested.commands) - set(policy.allowed_commands)
    )
    if missing_commands:
        raise _fail(
            "SDAI-PLUGIN-003",
            "command permission denied: " + ", ".join(missing_commands),
        )

    normalized = _validate_json_inputs(
        inputs or {},
        label=f"plugin step '{step_id}' inputs",
    )
    assert isinstance(normalized, dict)
    return PluginExecutionPlan(
        plugin=plugin,
        step_id=_safe_id(step_id, "plugin step id"),
        inputs=normalized,
        permissions=requested,
        policy_sources=policy.sources,
    )


_PROTECTED_PREFIXES = (
    ".git",
    ".sdai",
    ".agents",
    ".codex",
    ".claude",
    ".gemini",
    ".github/workflows",
    ".github/agents",
    "specs",
)
_PROTECTED_FILES = frozenset({"CODEOWNERS"})


def _is_protected(relative: str) -> bool:
    normalized = relative.strip("/")
    if normalized in _PROTECTED_FILES:
        return True
    return any(
        normalized == prefix or normalized.startswith(prefix + "/")
        for prefix in _PROTECTED_PREFIXES
    )


class PluginExecutionServices:
    """Framework services exposed to trusted executor implementations.

    The executor implementation itself is trusted installed code. These services
    enforce the manifest/policy contract for framework-mediated I/O/commands; the
    YAML manifest cannot load code, obtain a raw shell, or bypass protected writes.
    """

    def __init__(
        self,
        project_root: Path,
        plan: PluginExecutionPlan,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.root = project_root.resolve()
        self.plan = plan
        self.environ = dict(os.environ if environ is None else environ)

    def _relative(
        self,
        value: str,
        permission_paths: tuple[str, ...],
        operation: str,
    ) -> tuple[Path, str]:
        relative = _safe_repo_path(value, f"plugin {operation} path")
        if not _path_allowed(relative, permission_paths):
            raise _fail(
                "SDAI-PLUGIN-005",
                f"plugin did not declare {operation} permission for '{relative}'",
            )
        path = ensure_within_project(
            self.root,
            self.root / Path(*relative.split("/")),
            label=f"plugin {operation} path",
        )
        return path, relative

    def read_text(self, relative_path: str) -> str:
        path, _ = self._relative(
            relative_path,
            self.plan.permissions.filesystem_read,
            "read",
        )
        if path.is_symlink() or not path.is_file():
            raise _fail(
                "SDAI-PLUGIN-005",
                "plugin read target must be a regular non-symlink file: "
                + relative_path,
            )
        return read_utf8_text(path)

    def write_text(self, relative_path: str, content: str) -> None:
        if not self.plan.permissions.workspace_write:
            raise _fail(
                "SDAI-PLUGIN-005",
                "plugin did not declare workspace_write permission",
            )
        path, relative = self._relative(
            relative_path,
            self.plan.permissions.filesystem_write,
            "write",
        )
        if _is_protected(relative):
            raise _fail(
                "SDAI-PLUGIN-005",
                f"plugin cannot write protected path '{relative}'",
            )
        if path.exists() and path.is_symlink():
            raise _fail(
                "SDAI-PLUGIN-005",
                f"plugin cannot write through symlink '{relative}'",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    def getenv(self, name: str) -> str | None:
        if name not in self.plan.permissions.environment:
            raise _fail(
                "SDAI-PLUGIN-005",
                f"plugin did not declare environment permission '{name}'",
            )
        return self.environ.get(name)

    def run_argv(
        self,
        executable: str,
        argv: list[str] | tuple[str, ...],
    ) -> subprocess.CompletedProcess[str]:
        if executable not in self.plan.permissions.commands:
            raise _fail(
                "SDAI-PLUGIN-005",
                f"plugin did not declare command permission '{executable}'",
            )
        if not _EXECUTABLE.fullmatch(executable):
            raise _fail(
                "SDAI-PLUGIN-005",
                "plugin executable must be a bare executable name",
            )
        if not isinstance(argv, (list, tuple)) or not all(
            isinstance(item, str) for item in argv
        ):
            raise _fail(
                "SDAI-PLUGIN-005",
                "plugin argv must be a string list",
            )
        if any(
            "\x00" in item
            or "\n" in item
            or "\r" in item
            or _FORBIDDEN_TEMPLATE.search(item)
            for item in argv
        ):
            raise _fail(
                "SDAI-PLUGIN-005",
                "plugin argv contains unsafe NUL/newline/template syntax",
            )
        resolved = shutil.which(executable)
        if resolved is None:
            raise _fail(
                "SDAI-PLUGIN-005",
                f"allowed executable '{executable}' was not found",
            )

        child_environment = {
            name: self.environ[name]
            for name in self.plan.permissions.environment
            if name in self.environ
        }
        if os.name == "nt":
            for name in ("SYSTEMROOT", "WINDIR"):
                if name in self.environ:
                    child_environment.setdefault(name, self.environ[name])

        return subprocess.run(
            [resolved, *argv],
            cwd=self.root,
            env=child_environment,
            shell=False,
            text=True,
            capture_output=True,
            check=False,
        )


def execute_plugin_step(
    project_root: Path,
    plugin_id: str,
    step_id: str,
    inputs: Mapping[str, object] | None = None,
    *,
    registry: PluginExecutorRegistry = EXECUTORS,
    environ: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> tuple[PluginExecutionPlan, PluginResult | None]:
    plan = prepare_plugin_step(
        project_root,
        plugin_id,
        step_id,
        inputs,
        environ=environ,
    )
    if dry_run:
        return plan, None

    executor = registry.get(plan.plugin.executor)
    if executor is None:
        raise _fail(
            "SDAI-PLUGIN-006",
            f"trusted plugin executor '{plan.plugin.executor}' is not registered",
        )
    services = PluginExecutionServices(
        project_root,
        plan,
        environ=environ,
    )
    result = executor.execute(plan, services)
    if not isinstance(result, PluginResult):
        raise _fail(
            "SDAI-PLUGIN-006",
            "plugin executor returned an invalid result type",
        )
    if result.status not in {"passed", "failed"}:
        raise _fail(
            "SDAI-PLUGIN-006",
            "plugin result status must be passed or failed",
        )
    return plan, result
