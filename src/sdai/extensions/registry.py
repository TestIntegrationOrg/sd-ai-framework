from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from sdai.extensions.manifests import ExtensionKind, ExtensionManifest


class ExtensionRegistryError(RuntimeError):
    """Raised when extension registration would make resolution ambiguous or unsafe."""


class RegistryLayer(StrEnum):
    BUILTIN = "builtin"
    PACK = "pack"
    ORG = "org"
    REPO = "repo"
    USER = "user"

    @property
    def priority(self) -> int:
        return _LAYER_PRIORITY[self]


_LAYER_PRIORITY = {
    RegistryLayer.BUILTIN: 0,
    RegistryLayer.PACK: 10,
    RegistryLayer.ORG: 20,
    RegistryLayer.REPO: 30,
    RegistryLayer.USER: 40,
}
_LOCKABLE_LAYERS = frozenset({RegistryLayer.BUILTIN, RegistryLayer.ORG})


@dataclass(frozen=True)
class ExtensionKey:
    kind: ExtensionKind
    id: str


@dataclass(frozen=True)
class RegistryEntry:
    manifest: ExtensionManifest
    layer: RegistryLayer
    source: str
    path: Path | None = None
    locked: bool = False

    @property
    def key(self) -> ExtensionKey:
        return ExtensionKey(self.manifest.kind, self.manifest.metadata.id)


class ExtensionRegistry:
    """Resolve extension definitions across SDAI's layered configuration model.

    Normal precedence is ``builtin < pack < org < repo < user``. Built-in and
    organization entries may be marked ``locked``; a locked entry prevents any
    normally higher-precedence layer from replacing that definition.

    Duplicate definitions in the same layer and attempted overrides of a locked
    definition fail closed. Unlocked resolution is independent of registration
    order. Registry builders must load authoritative locked layers before layers
    they protect so a policy error aborts construction rather than leaving a
    partially built registry usable by mistake.
    """

    def __init__(self) -> None:
        self._entries: dict[ExtensionKey, dict[RegistryLayer, RegistryEntry]] = {}

    def register(
        self,
        manifest: ExtensionManifest,
        *,
        layer: RegistryLayer,
        source: str | None = None,
        path: Path | None = None,
        locked: bool = False,
    ) -> RegistryEntry:
        try:
            layer = RegistryLayer(layer)
        except ValueError as exc:
            supported = ", ".join(item.value for item in RegistryLayer)
            raise ExtensionRegistryError(
                f"SDAI-REG-004: unknown registry layer {layer!r}; supported layers: {supported}"
            ) from exc

        if locked and layer not in _LOCKABLE_LAYERS:
            allowed = ", ".join(
                item.value
                for item in sorted(_LOCKABLE_LAYERS, key=lambda item: item.priority)
            )
            raise ExtensionRegistryError(
                "SDAI-REG-002: locked definitions are only allowed in "
                f"authoritative layers: {allowed}"
            )

        provenance_source = source.strip() if source is not None else manifest.source
        if not provenance_source:
            provenance_source = manifest.source
        entry = RegistryEntry(
            manifest=manifest,
            layer=layer,
            source=provenance_source,
            path=path,
            locked=locked,
        )
        key = entry.key
        layer_entries = self._entries.get(key, {})
        if layer in layer_entries:
            existing = layer_entries[layer]
            raise ExtensionRegistryError(
                "SDAI-REG-001: duplicate extension definition for "
                f"{key.kind.value}/{key.id} in layer '{layer.value}' "
                f"(existing source: {existing.source}; new source: {entry.source})"
            )

        candidate = dict(layer_entries)
        candidate[layer] = entry
        self._validate_lock_chain(key, candidate.values())
        self._entries[key] = candidate
        return entry

    def resolve(self, kind: ExtensionKind, extension_id: str) -> RegistryEntry | None:
        entries = self._entries.get(ExtensionKey(kind, extension_id))
        if not entries:
            return None
        ordered = sorted(entries.values(), key=lambda item: item.layer.priority)
        self._validate_lock_chain(ExtensionKey(kind, extension_id), ordered)
        return ordered[-1]

    def history(self, kind: ExtensionKind, extension_id: str) -> tuple[RegistryEntry, ...]:
        entries = self._entries.get(ExtensionKey(kind, extension_id), {})
        ordered = tuple(sorted(entries.values(), key=lambda item: item.layer.priority))
        self._validate_lock_chain(ExtensionKey(kind, extension_id), ordered)
        return ordered

    def list_resolved(self, kind: ExtensionKind | None = None) -> tuple[RegistryEntry, ...]:
        keys = sorted(
            (key for key in self._entries if kind is None or key.kind == kind),
            key=lambda item: (item.kind.value, item.id),
        )
        return tuple(
            entry
            for key in keys
            if (entry := self.resolve(key.kind, key.id)) is not None
        )

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _validate_lock_chain(key: ExtensionKey, entries: Iterable[RegistryEntry]) -> None:
        ordered = sorted(entries, key=lambda item: item.layer.priority)
        locked_entry: RegistryEntry | None = None
        for entry in ordered:
            if locked_entry is not None:
                raise ExtensionRegistryError(
                    "SDAI-REG-003: extension override is blocked for "
                    f"{key.kind.value}/{key.id}; layer '{entry.layer.value}' from "
                    f"'{entry.source}' cannot override locked layer "
                    f"'{locked_entry.layer.value}' from '{locked_entry.source}'"
                )
            if entry.locked:
                locked_entry = entry
