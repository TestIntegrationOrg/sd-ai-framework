from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sdai.agent_platform.skills import load_skill
from sdai.evals import load_eval_scenarios
from sdai.extensions.manifests import ExtensionKind, load_extension_manifest
from sdai.path_safety import ensure_within_project
from sdai.skill_resolution import SkillMetadata, load_skill_metadata


class LanguagePackError(RuntimeError):
    pass


TIER1_LANGUAGE_PACK_IDS: tuple[str, ...] = (
    "sdai-java",
    "sdai-dotnet",
    "sdai-python",
    "sdai-typescript-javascript",
    "sdai-go",
    "sdai-powershell",
)


@dataclass(frozen=True)
class LanguagePack:
    id: str
    version: str
    languages: tuple[str, ...]
    core_skills: tuple[str, ...]
    framework_skills: tuple[str, ...]
    source: str

    @property
    def skills(self) -> tuple[str, ...]:
        return (*self.core_skills, *self.framework_skills)


_SPEC_KEYS = frozenset({"type", "languages", "skills"})
_SKILL_KEYS = frozenset({"core", "frameworks"})


def _fail(code: str, message: str) -> LanguagePackError:
    return LanguagePackError(f"{code}: {message}")


def _portable(root: Path, path: Path) -> str:
    safe = ensure_within_project(root, path, label="language pack path")
    return safe.relative_to(root.resolve()).as_posix()


def _strings(value: object, *, label: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise _fail("SDAI-LANGPACK-001", f"{label} must be a string list")
    values = tuple(item.strip() for item in value)
    if not allow_empty and not values:
        raise _fail("SDAI-LANGPACK-001", f"{label} must not be empty")
    if len(set(values)) != len(values):
        raise _fail("SDAI-LANGPACK-001", f"{label} must not contain duplicates")
    return values


def _load_pack_skill(
    root: Path,
    pack_id: str,
    name: str,
    group: str,
) -> SkillMetadata:
    try:
        load_skill(root, name)
        metadata = load_skill_metadata(root, name)
        load_eval_scenarios(root, "skill", name)
    except RuntimeError as exc:
        raise _fail(
            "SDAI-LANGPACK-002",
            f"pack '{pack_id}' {group} skill '{name}' is invalid: {exc}",
        ) from exc
    if not metadata.selection.auto:
        raise _fail(
            "SDAI-LANGPACK-003",
            f"pack '{pack_id}' {group} skill '{name}' must opt into automatic selection",
        )
    if not metadata.capabilities:
        raise _fail(
            "SDAI-LANGPACK-003",
            f"pack '{pack_id}' {group} skill '{name}' must declare lifecycle capabilities",
        )
    return metadata


def load_language_pack(project_root: Path, pack_id: str) -> LanguagePack:
    root = project_root.resolve()
    path = ensure_within_project(
        root,
        root / ".sdai" / "extensions" / "packs" / f"{pack_id}.yaml",
        label="language pack manifest",
    )
    try:
        manifest = load_extension_manifest(root, path)
    except RuntimeError as exc:
        raise _fail(
            "SDAI-LANGPACK-001",
            f"invalid language pack manifest '{_portable(root, path)}': {exc}",
        ) from exc
    if manifest.kind is not ExtensionKind.PACK:
        raise _fail(
            "SDAI-LANGPACK-001",
            f"{_portable(root, path)} must be a Pack manifest",
        )
    if manifest.metadata.id != pack_id:
        raise _fail(
            "SDAI-LANGPACK-001",
            f"pack manifest id '{manifest.metadata.id}' must match requested/file id '{pack_id}'",
        )
    unknown = sorted(set(manifest.spec) - _SPEC_KEYS)
    if unknown:
        raise _fail(
            "SDAI-LANGPACK-001",
            f"pack '{pack_id}' contains unsupported spec key(s): {', '.join(unknown)}",
        )
    if manifest.spec.get("type") != "language":
        raise _fail(
            "SDAI-LANGPACK-001",
            f"pack '{pack_id}' spec.type must be 'language'",
        )
    languages = _strings(
        manifest.spec.get("languages"),
        label=f"pack '{pack_id}' languages",
    )
    raw_skills = manifest.spec.get("skills")
    if not isinstance(raw_skills, dict):
        raise _fail(
            "SDAI-LANGPACK-001",
            f"pack '{pack_id}' spec.skills must be a mapping",
        )
    unknown_skill_keys = sorted(set(raw_skills) - _SKILL_KEYS)
    if unknown_skill_keys:
        raise _fail(
            "SDAI-LANGPACK-001",
            f"pack '{pack_id}' contains unsupported skill group(s): "
            + ", ".join(unknown_skill_keys),
        )
    core = _strings(
        raw_skills.get("core"),
        label=f"pack '{pack_id}' skills.core",
    )
    frameworks = _strings(
        raw_skills.get("frameworks", []),
        label=f"pack '{pack_id}' skills.frameworks",
        allow_empty=True,
    )
    overlap = sorted(set(core).intersection(frameworks))
    if overlap:
        raise _fail(
            "SDAI-LANGPACK-001",
            f"pack '{pack_id}' repeats skill(s) across core/frameworks: {', '.join(overlap)}",
        )

    core_metadata: dict[str, SkillMetadata] = {}
    for name in core:
        metadata = _load_pack_skill(root, pack_id, name, "core")
        language_rules = metadata.compatibility.get("languages", {})
        if not set(language_rules).intersection(languages):
            raise _fail(
                "SDAI-LANGPACK-003",
                f"core skill '{name}' in '{pack_id}' must declare compatibility with one of: "
                + ", ".join(languages),
            )
        core_metadata[name] = metadata

    for name in frameworks:
        metadata = _load_pack_skill(root, pack_id, name, "framework")
        if not metadata.compatibility.get("frameworks"):
            raise _fail(
                "SDAI-LANGPACK-003",
                f"framework skill '{name}' in '{pack_id}' must declare framework compatibility",
            )
        if not set(metadata.requires).intersection(core_metadata):
            raise _fail(
                "SDAI-LANGPACK-004",
                f"framework skill '{name}' in '{pack_id}' must depend on a core pack skill",
            )

    return LanguagePack(
        id=manifest.metadata.id,
        version=manifest.metadata.version,
        languages=languages,
        core_skills=core,
        framework_skills=frameworks,
        source=_portable(root, path),
    )


def list_language_packs(project_root: Path) -> tuple[LanguagePack, ...]:
    root = project_root.resolve()
    directory = ensure_within_project(
        root,
        root / ".sdai" / "extensions" / "packs",
        label="language pack directory",
    )
    if not directory.exists():
        return ()
    result: list[LanguagePack] = []
    for path in sorted(
        [*directory.glob("*.yaml"), *directory.glob("*.yml")],
        key=lambda item: item.name.casefold(),
    ):
        try:
            manifest = load_extension_manifest(root, path)
        except RuntimeError as exc:
            raise _fail(
                "SDAI-LANGPACK-001",
                f"invalid extension manifest '{_portable(root, path)}': {exc}",
            ) from exc
        if manifest.kind is not ExtensionKind.PACK or manifest.spec.get("type") != "language":
            continue
        result.append(load_language_pack(root, path.stem))
    return tuple(result)


def validate_tier1_language_packs(project_root: Path) -> tuple[LanguagePack, ...]:
    discovered = {pack.id: pack for pack in list_language_packs(project_root)}
    missing = [pack_id for pack_id in TIER1_LANGUAGE_PACK_IDS if pack_id not in discovered]
    if missing:
        raise _fail(
            "SDAI-LANGPACK-005",
            "missing Tier-1 language pack(s): " + ", ".join(missing),
        )
    return tuple(discovered[pack_id] for pack_id in TIER1_LANGUAGE_PACK_IDS)
