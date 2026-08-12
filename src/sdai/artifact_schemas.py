from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Mapping

import yaml

from sdai.extensions.manifests import (
    ExtensionKind,
    ExtensionManifestError,
    parse_extension_manifest,
)
from sdai.path_safety import ensure_within_project
from sdai.text import TextEncodingError, read_utf8_text


class ArtifactSchemaError(RuntimeError):
    """Raised when the effective artifact schema cannot be resolved safely."""


class ArtifactSchemaLayer(StrEnum):
    BUILTIN = "builtin"
    ORG = "org"
    REPO = "repo"
    USER = "user"

    @property
    def priority(self) -> int:
        return {
            ArtifactSchemaLayer.BUILTIN: 0,
            ArtifactSchemaLayer.ORG: 10,
            ArtifactSchemaLayer.REPO: 20,
            ArtifactSchemaLayer.USER: 30,
        }[self]


@dataclass(frozen=True)
class ArtifactSchemaContribution:
    layer: ArtifactSchemaLayer
    schema_id: str
    source: str
    fields: tuple[str, ...]
    disabled: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer.value,
            "schema_id": self.schema_id,
            "source": self.source,
            "fields": list(self.fields),
            "disabled": self.disabled,
        }


@dataclass(frozen=True)
class ArtifactDefinition:
    id: str
    path: str
    type: str
    required: bool
    locked: bool
    depends_on: tuple[str, ...]
    applies_to: tuple[str, ...]
    source_layer: ArtifactSchemaLayer
    source_schema: str
    source: str
    history: tuple[ArtifactSchemaContribution, ...]
    organization_required: bool = False
    organization_dependencies: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "path": self.path,
            "type": self.type,
            "required": self.required,
            "locked": self.locked,
            "depends_on": list(self.depends_on),
            "applies_to": list(self.applies_to),
            "organization_required": self.organization_required,
            "organization_dependencies": list(self.organization_dependencies),
            "source_layer": self.source_layer.value,
            "source_schema": self.source_schema,
            "source": self.source,
            "history": [item.as_dict() for item in self.history],
        }


@dataclass(frozen=True)
class ArtifactSchemaGraph:
    artifacts: tuple[ArtifactDefinition, ...]
    topological_order: tuple[str, ...]
    sources: tuple[str, ...]

    def by_id(self) -> dict[str, ArtifactDefinition]:
        return {item.id: item for item in self.artifacts}

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "artifacts": [item.as_dict() for item in self.artifacts],
            "topological_order": list(self.topological_order),
            "edges": [
                {"from": dependency, "to": artifact.id}
                for artifact in self.artifacts
                for dependency in artifact.depends_on
            ],
            "sources": list(self.sources),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class _ArtifactOverlay:
    id: str
    layer: ArtifactSchemaLayer
    schema_id: str
    source: str
    fields: Mapping[str, object]


_ARTIFACT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_ALLOWED_SPEC_KEYS = frozenset({"artifacts"})
_ALLOWED_ARTIFACT_KEYS = frozenset(
    {
        "id",
        "path",
        "type",
        "required",
        "locked",
        "disabled",
        "depends_on",
        "applies_to",
    }
)
_ARTIFACT_TYPES = frozenset(
    {
        "markdown",
        "yaml",
        "json",
        "text",
        "directory",
        "openapi",
        "asyncapi",
        "json-schema",
        "protobuf",
        "drawio",
        "plantuml",
    }
)
_RISKS = frozenset({"trivial", "standard", "critical", "regulated"})
_PLACEHOLDERS = frozenset({"feature", "domain"})
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_DOS_DEVICE = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$",
    re.IGNORECASE,
)


def _fail(code: str, message: str) -> ArtifactSchemaError:
    return ArtifactSchemaError(f"{code}: {message}")


def _portable(root: Path, path: Path) -> str:
    safe = ensure_within_project(root, path, label="artifact schema path")
    return safe.relative_to(root.resolve()).as_posix()


def _external_source(path: Path) -> str:
    return path.resolve().as_posix()


def _string_list(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise _fail("SDAI-SCHEMA-001", f"{label} must be a string list")
    values = tuple(item.strip() for item in value)
    if len(values) != len(set(values)):
        raise _fail("SDAI-SCHEMA-001", f"{label} must not contain duplicates")
    return values


def _validate_portable_segment(segment: str, *, label: str) -> None:
    if not segment or segment in {".", ".."}:
        raise _fail("SDAI-SCHEMA-002", f"{label} contains an invalid path segment")
    if ":" in segment or segment.endswith((".", " ")):
        raise _fail(
            "SDAI-SCHEMA-002",
            f"{label} contains a path segment that is not portable across Windows/Linux",
        )
    if any(ord(char) < 32 for char in segment):
        raise _fail("SDAI-SCHEMA-002", f"{label} contains a control character")
    if _DOS_DEVICE.fullmatch(segment):
        raise _fail(
            "SDAI-SCHEMA-002",
            f"{label} contains reserved Windows device name '{segment}'",
        )


def _validate_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(
            "SDAI-SCHEMA-002",
            f"{label} must be a non-empty POSIX repository-relative path",
        )
    text = value.strip()
    if "\\" in text or re.match(r"^[A-Za-z]:", text) or text.startswith("/"):
        raise _fail(
            "SDAI-SCHEMA-002",
            f"{label} must be a POSIX repository-relative path",
        )
    placeholders = set(_PLACEHOLDER.findall(text))
    unknown = sorted(placeholders - _PLACEHOLDERS)
    if unknown:
        raise _fail(
            "SDAI-SCHEMA-002",
            f"{label} contains unsupported placeholder(s): {', '.join(unknown)}",
        )
    scrubbed = _PLACEHOLDER.sub("x", text)
    if "{" in scrubbed or "}" in scrubbed:
        raise _fail(
            "SDAI-SCHEMA-002",
            f"{label} contains malformed placeholder syntax",
        )
    path = PurePosixPath(scrubbed)
    if path.is_absolute():
        raise _fail(
            "SDAI-SCHEMA-002",
            f"{label} must be repository-relative",
        )
    for segment in path.parts:
        _validate_portable_segment(segment, label=label)
    return text


def _manifest_from_file(path: Path, *, source: str):
    try:
        raw = yaml.safe_load(read_utf8_text(path)) or {}
    except (OSError, TextEncodingError, yaml.YAMLError) as exc:
        raise _fail(
            "SDAI-SCHEMA-001",
            f"unable to read artifact schema {source}: {exc}",
        ) from exc
    if not isinstance(raw, Mapping):
        raise _fail(
            "SDAI-SCHEMA-001",
            f"artifact schema {source} must be a YAML mapping",
        )
    try:
        manifest = parse_extension_manifest(raw, source=source)
    except ExtensionManifestError as exc:
        raise _fail(
            "SDAI-SCHEMA-001",
            f"invalid artifact schema {source}: {exc}",
        ) from exc
    if manifest.kind is not ExtensionKind.ARTIFACT_SCHEMA:
        raise _fail(
            "SDAI-SCHEMA-001",
            f"artifact schema {source} kind must be {ExtensionKind.ARTIFACT_SCHEMA.value}",
        )
    unknown = sorted(set(manifest.spec) - _ALLOWED_SPEC_KEYS)
    if unknown:
        raise _fail(
            "SDAI-SCHEMA-001",
            f"artifact schema {source} contains unsupported spec key(s): {', '.join(unknown)}",
        )
    return manifest


def _parse_manifest_overlays(
    path: Path,
    *,
    layer: ArtifactSchemaLayer,
    source: str,
) -> tuple[_ArtifactOverlay, ...]:
    manifest = _manifest_from_file(path, source=source)
    raw_artifacts = manifest.spec.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise _fail(
            "SDAI-SCHEMA-001",
            f"artifact schema {source} spec.artifacts must be a list",
        )
    overlays: list[_ArtifactOverlay] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_artifacts, start=1):
        if not isinstance(raw, Mapping):
            raise _fail(
                "SDAI-SCHEMA-001",
                f"artifact schema {source} artifact #{index} must be a mapping",
            )
        unknown = sorted(set(raw) - _ALLOWED_ARTIFACT_KEYS)
        if unknown:
            raise _fail(
                "SDAI-SCHEMA-001",
                f"artifact schema {source} artifact #{index} has unsupported key(s): {', '.join(unknown)}",
            )
        artifact_id = raw.get("id")
        if (
            not isinstance(artifact_id, str)
            or not _ARTIFACT_ID.fullmatch(artifact_id)
            or ".." in artifact_id
        ):
            raise _fail(
                "SDAI-SCHEMA-001",
                f"artifact schema {source} artifact #{index} has invalid id {artifact_id!r}",
            )
        if artifact_id in seen:
            raise _fail(
                "SDAI-SCHEMA-003",
                f"artifact schema {source} defines artifact '{artifact_id}' more than once",
            )
        seen.add(artifact_id)
        fields = {str(key): value for key, value in raw.items() if key != "id"}
        for boolean_field in ("required", "locked", "disabled"):
            if boolean_field in fields and not isinstance(fields[boolean_field], bool):
                raise _fail(
                    "SDAI-SCHEMA-001",
                    f"artifact '{artifact_id}' field '{boolean_field}' must be true or false",
                )
        if fields.get("locked") and layer not in {
            ArtifactSchemaLayer.BUILTIN,
            ArtifactSchemaLayer.ORG,
        }:
            raise _fail(
                "SDAI-SCHEMA-004",
                f"artifact '{artifact_id}' cannot be locked from non-authoritative layer '{layer.value}'",
            )
        if "path" in fields:
            fields["path"] = _validate_path(
                fields["path"],
                label=f"artifact '{artifact_id}' path",
            )
        if "type" in fields:
            artifact_type = fields["type"]
            if artifact_type not in _ARTIFACT_TYPES:
                raise _fail(
                    "SDAI-SCHEMA-001",
                    f"artifact '{artifact_id}' has unsupported type {artifact_type!r}",
                )
        if "depends_on" in fields:
            dependencies = _string_list(
                fields["depends_on"],
                label=f"artifact '{artifact_id}' depends_on",
            )
            for dependency in dependencies:
                if not _ARTIFACT_ID.fullmatch(dependency) or ".." in dependency:
                    raise _fail(
                        "SDAI-SCHEMA-001",
                        f"artifact '{artifact_id}' has invalid dependency id '{dependency}'",
                    )
                if dependency == artifact_id:
                    raise _fail(
                        "SDAI-SCHEMA-006",
                        f"artifact '{artifact_id}' cannot depend on itself",
                    )
            fields["depends_on"] = dependencies
        if "applies_to" in fields:
            risks = _string_list(
                fields["applies_to"],
                label=f"artifact '{artifact_id}' applies_to",
            )
            unsupported = sorted(set(risks) - _RISKS)
            if unsupported:
                raise _fail(
                    "SDAI-SCHEMA-001",
                    f"artifact '{artifact_id}' has unsupported risk profile(s): {', '.join(unsupported)}",
                )
            fields["applies_to"] = risks
        overlays.append(
            _ArtifactOverlay(
                id=artifact_id,
                layer=layer,
                schema_id=manifest.metadata.id,
                source=source,
                fields=fields,
            )
        )
    return tuple(overlays)


def _external_paths(value: str | None, *, label: str) -> tuple[Path, ...]:
    if not value:
        return ()
    path = Path(value)
    if not path.is_absolute():
        raise _fail(
            "SDAI-SCHEMA-008",
            f"{label} must be an absolute file or directory path",
        )
    if path.is_symlink():
        raise _fail("SDAI-SCHEMA-008", f"{label} must not be a symlink")
    if path.is_file():
        return (path,)
    if path.is_dir():
        candidates = sorted(
            [*path.glob("*.yaml"), *path.glob("*.yml")],
            key=lambda item: item.name.casefold(),
        )
        for candidate in candidates:
            if candidate.is_symlink() or not candidate.is_file():
                raise _fail(
                    "SDAI-SCHEMA-008",
                    f"{label} schema file must be a regular non-symlink file: {candidate}",
                )
        return tuple(candidates)
    raise _fail("SDAI-SCHEMA-008", f"{label} does not exist: {path}")


def _layer_files(
    project_root: Path,
    *,
    environ: Mapping[str, str],
) -> tuple[tuple[ArtifactSchemaLayer, Path, str], ...]:
    root = project_root.resolve()
    builtin = Path(__file__).resolve().parent / "builtin_schemas"
    result: list[tuple[ArtifactSchemaLayer, Path, str]] = []
    for path in sorted(builtin.glob("*.yaml"), key=lambda item: item.name.casefold()):
        result.append(
            (ArtifactSchemaLayer.BUILTIN, path, f"builtin:{path.name}")
        )

    for path in _external_paths(
        environ.get("SDAI_ORG_SCHEMA_PATH"),
        label="SDAI_ORG_SCHEMA_PATH",
    ):
        result.append(
            (ArtifactSchemaLayer.ORG, path, _external_source(path))
        )

    repo_dir = ensure_within_project(
        root,
        root / ".sdai" / "schemas",
        label="repository schema directory",
    )
    if repo_dir.exists():
        if repo_dir.is_symlink() or not repo_dir.is_dir():
            raise _fail(
                "SDAI-SCHEMA-008",
                ".sdai/schemas must be a real directory",
            )
        for path in sorted(
            [*repo_dir.glob("*.yaml"), *repo_dir.glob("*.yml")],
            key=lambda item: item.name.casefold(),
        ):
            if path.is_symlink() or not path.is_file():
                raise _fail(
                    "SDAI-SCHEMA-008",
                    f"repository schema must be a regular non-symlink file: {_portable(root, path)}",
                )
            result.append(
                (ArtifactSchemaLayer.REPO, path, _portable(root, path))
            )

    for path in _external_paths(
        environ.get("SDAI_USER_SCHEMA_PATH"),
        label="SDAI_USER_SCHEMA_PATH",
    ):
        result.append(
            (ArtifactSchemaLayer.USER, path, _external_source(path))
        )
    return tuple(result)


def _contribution(overlay: _ArtifactOverlay) -> ArtifactSchemaContribution:
    return ArtifactSchemaContribution(
        layer=overlay.layer,
        schema_id=overlay.schema_id,
        source=overlay.source,
        fields=tuple(sorted(overlay.fields)),
        disabled=bool(overlay.fields.get("disabled", False)),
    )


def _merge_overlay(
    existing: ArtifactDefinition | None,
    overlay: _ArtifactOverlay,
) -> ArtifactDefinition | None:
    fields = overlay.fields
    contribution = _contribution(overlay)

    if existing is None:
        if fields.get("disabled"):
            raise _fail(
                "SDAI-SCHEMA-004",
                f"artifact '{overlay.id}' cannot be disabled before it is defined",
            )
        if "path" not in fields or "type" not in fields:
            raise _fail(
                "SDAI-SCHEMA-001",
                f"new artifact '{overlay.id}' must define path and type",
            )
        dependencies = tuple(fields.get("depends_on", ()))
        required = bool(fields.get("required", False))
        is_org = overlay.layer is ArtifactSchemaLayer.ORG
        return ArtifactDefinition(
            id=overlay.id,
            path=str(fields["path"]),
            type=str(fields["type"]),
            required=required,
            locked=bool(fields.get("locked", False)),
            depends_on=dependencies,
            applies_to=tuple(
                fields.get(
                    "applies_to",
                    ("standard", "critical", "regulated"),
                )
            ),
            source_layer=overlay.layer,
            source_schema=overlay.schema_id,
            source=overlay.source,
            history=(contribution,),
            organization_required=is_org and required,
            organization_dependencies=(dependencies if is_org else ()),
        )

    if existing.locked:
        raise _fail(
            "SDAI-SCHEMA-004",
            f"artifact '{overlay.id}' from {overlay.layer.value} cannot override locked "
            f"{existing.source_layer.value} definition from {existing.source}",
        )

    if fields.get("disabled"):
        if existing.organization_required:
            raise _fail(
                "SDAI-SCHEMA-004",
                f"artifact '{overlay.id}' is required by organization schema and cannot be disabled",
            )
        return None

    required = (
        existing.required
        if "required" not in fields
        else bool(fields["required"])
    )
    if existing.organization_required and not required:
        raise _fail(
            "SDAI-SCHEMA-004",
            f"artifact '{overlay.id}' is required by organization schema and cannot be made optional",
        )

    dependencies = (
        existing.depends_on
        if "depends_on" not in fields
        else tuple(fields["depends_on"])
    )
    if not set(existing.organization_dependencies).issubset(dependencies):
        missing = sorted(
            set(existing.organization_dependencies) - set(dependencies)
        )
        raise _fail(
            "SDAI-SCHEMA-004",
            f"artifact '{overlay.id}' cannot remove organization dependency/dependencies: {', '.join(missing)}",
        )

    organization_required = existing.organization_required
    organization_dependencies = existing.organization_dependencies
    if overlay.layer is ArtifactSchemaLayer.ORG:
        if bool(fields.get("required", False)):
            organization_required = True
        if "depends_on" in fields:
            organization_dependencies = tuple(
                sorted(
                    set(organization_dependencies) | set(dependencies)
                )
            )

    return ArtifactDefinition(
        id=existing.id,
        path=(
            existing.path
            if "path" not in fields
            else str(fields["path"])
        ),
        type=(
            existing.type
            if "type" not in fields
            else str(fields["type"])
        ),
        required=required,
        locked=(
            existing.locked
            if "locked" not in fields
            else bool(fields["locked"])
        ),
        depends_on=dependencies,
        applies_to=(
            existing.applies_to
            if "applies_to" not in fields
            else tuple(fields["applies_to"])
        ),
        source_layer=overlay.layer,
        source_schema=overlay.schema_id,
        source=overlay.source,
        history=(*existing.history, contribution),
        organization_required=organization_required,
        organization_dependencies=organization_dependencies,
    )


def _topological_order(
    artifacts: Mapping[str, ArtifactDefinition],
) -> tuple[str, ...]:
    for artifact in artifacts.values():
        for dependency in artifact.depends_on:
            if dependency not in artifacts:
                raise _fail(
                    "SDAI-SCHEMA-005",
                    f"artifact '{artifact.id}' depends on missing artifact '{dependency}'",
                )

    visiting: list[str] = []
    visited: set[str] = set()
    result: list[str] = []

    def visit(artifact_id: str) -> None:
        if artifact_id in visited:
            return
        if artifact_id in visiting:
            cycle = visiting[visiting.index(artifact_id) :] + [artifact_id]
            raise _fail(
                "SDAI-SCHEMA-006",
                "artifact dependency cycle: " + " -> ".join(cycle),
            )
        visiting.append(artifact_id)
        for dependency in sorted(artifacts[artifact_id].depends_on):
            visit(dependency)
        visiting.pop()
        visited.add(artifact_id)
        result.append(artifact_id)

    for artifact_id in sorted(artifacts):
        visit(artifact_id)
    return tuple(result)


def load_artifact_schema_graph(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> ArtifactSchemaGraph:
    root = project_root.resolve()
    env = dict(os.environ if environ is None else environ)
    effective: dict[str, ArtifactDefinition] = {}
    seen_by_layer: dict[ArtifactSchemaLayer, dict[str, str]] = {
        layer: {} for layer in ArtifactSchemaLayer
    }
    sources: list[str] = []

    for layer, path, source in _layer_files(root, environ=env):
        sources.append(f"{layer.value}:{source}")
        for overlay in _parse_manifest_overlays(
            path,
            layer=layer,
            source=source,
        ):
            previous_source = seen_by_layer[layer].get(overlay.id)
            if previous_source is not None:
                raise _fail(
                    "SDAI-SCHEMA-003",
                    f"artifact '{overlay.id}' is defined more than once in layer '{layer.value}' "
                    f"({previous_source}; {source})",
                )
            seen_by_layer[layer][overlay.id] = source
            merged = _merge_overlay(effective.get(overlay.id), overlay)
            if merged is None:
                effective.pop(overlay.id, None)
            else:
                effective[overlay.id] = merged

    order = _topological_order(effective)
    artifacts = tuple(
        effective[artifact_id] for artifact_id in sorted(effective)
    )
    return ArtifactSchemaGraph(
        artifacts=artifacts,
        topological_order=order,
        sources=tuple(sources),
    )
