from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from sdai.extensions.manifests import ExtensionManifest, load_extension_manifest
from sdai.extensions.registry import (
    ExtensionRegistry,
    ExtensionRegistryError,
    RegistryEntry,
    RegistryLayer,
)


@dataclass(frozen=True)
class ExtensionSource:
    """A manifest file plus the containment root and registry layer that own it."""

    root: Path
    path: Path
    layer: RegistryLayer
    locked: bool = False
    label: str | None = None


def _coerce_layer(value: RegistryLayer) -> RegistryLayer:
    try:
        return RegistryLayer(value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in RegistryLayer)
        raise ExtensionRegistryError(
            f"SDAI-REG-004: unknown registry layer {value!r}; supported layers: {supported}"
        ) from exc


def _portable_path_key(path: Path) -> str:
    return str(path).replace("\\", "/").casefold()


def _source_sort_key(source: ExtensionSource) -> tuple[int, str, str, str]:
    layer = _coerce_layer(source.layer)
    return (
        layer.priority,
        _portable_path_key(source.root),
        _portable_path_key(source.path),
        (source.label or "").casefold(),
    )


def register_extension_source(
    registry: ExtensionRegistry,
    source: ExtensionSource,
) -> RegistryEntry:
    """Load and register one manifest while preserving file provenance."""

    layer = _coerce_layer(source.layer)
    manifest: ExtensionManifest = load_extension_manifest(source.root, source.path)
    manifest_path = Path(manifest.source)
    return registry.register(
        manifest,
        layer=layer,
        source=source.label or manifest.source,
        path=manifest_path,
        locked=source.locked,
    )


def build_extension_registry(sources: Iterable[ExtensionSource]) -> ExtensionRegistry:
    """Build a fresh registry from manifest sources in deterministic layer order.

    Sorting by the registry authority/precedence order ensures built-in and
    organization locks are installed before repository/user layers they protect,
    even when callers provide sources in an arbitrary order. Any validation,
    containment, duplicate, or lock violation aborts construction by raising an
    exception; no partially built registry is returned.
    """

    registry = ExtensionRegistry()
    for source in sorted(tuple(sources), key=_source_sort_key):
        register_extension_source(registry, source)
    return registry
