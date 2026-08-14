from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from sdai.pack_manifest import (
    PACK_MANIFEST_API_VERSION,
    PackManifest,
    PackManifestError,
    SemVer,
    VersionConstraint,
    load_pack_manifest,
)


def _manifest() -> dict[str, object]:
    return {
        "apiVersion": PACK_MANIFEST_API_VERSION,
        "id": "secure-coding",
        "publisher": "acme",
        "version": "1.2.3-rc.1+build.7",
        "description": "Secure café engineering Δ pack",
        "capabilities": ["workflows", "skills"],
        "contentRoots": ["workflows", "skills/café"],
        "dependencies": [
            {"publisher": "sdai", "id": "core-quality", "version": ">=1.0.0,<2.0.0"},
            {"publisher": "acme", "id": "shared-rules", "version": "=2.1.0"},
        ],
        "compatibility": {
            "framework": ">=0.5.4,<1.0.0",
            "apis": ["sdai.extension/v1", "sdai.pack-manifest/v1"],
        },
    }


def _write_layout(root: Path, raw: dict[str, object] | None = None) -> Path:
    (root / "workflows").mkdir(parents=True)
    (root / "skills" / "café").mkdir(parents=True)
    path = root / "pack.yaml"
    path.write_text(
        yaml.safe_dump(raw or _manifest(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_semver_20_precedence_and_build_metadata_rules() -> None:
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    ]
    parsed = [SemVer.parse(value) for value in ordered]

    for left, right in zip(parsed, parsed[1:]):
        assert left.compare_precedence(right) < 0
        assert left < right

    first_build = SemVer.parse("1.0.0+linux.1")
    second_build = SemVer.parse("1.0.0+windows.9")
    assert first_build.same_precedence(second_build)
    assert first_build.compare_precedence(second_build) == 0
    assert not first_build.exactly_equals(second_build)


@pytest.mark.parametrize(
    "value",
    [
        "1",
        "1.2",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-01",
        "1.2.3-",
        "v1.2.3",
        "1.2.3+bad_metadata!",
    ],
)
def test_semver_rejects_non_semver_or_ambiguous_versions(value: str) -> None:
    with pytest.raises(PackManifestError, match="SDAI-PACK-003"):
        SemVer.parse(value)


def test_version_constraints_are_canonical_and_match_by_semver_precedence() -> None:
    constraint = VersionConstraint.parse("<2.0.0, >=1.2.0")

    assert str(constraint) == ">=1.2.0,<2.0.0"
    assert constraint.matches("1.2.0")
    assert constraint.matches("1.9.9+build.4")
    assert not constraint.matches("2.0.0")
    assert not constraint.matches("1.1.9")
    assert VersionConstraint.parse("*").matches("99.0.0")

    exact_build = VersionConstraint.parse("=1.2.3+build.1")
    assert exact_build.matches("1.2.3+build.1")
    assert not exact_build.matches("1.2.3+build.2")


def test_manifest_serialization_hash_identity_and_semantic_sets_are_canonical() -> None:
    first_raw = _manifest()
    second_raw = deepcopy(first_raw)
    second_raw["capabilities"] = list(reversed(second_raw["capabilities"]))  # type: ignore[index]
    second_raw["contentRoots"] = list(reversed(second_raw["contentRoots"]))  # type: ignore[index]
    second_raw["dependencies"] = list(reversed(second_raw["dependencies"]))  # type: ignore[index]
    compatibility = second_raw["compatibility"]
    assert isinstance(compatibility, dict)
    compatibility["apis"] = list(reversed(compatibility["apis"]))
    second_raw = dict(reversed(list(second_raw.items())))

    first = PackManifest.from_dict(first_raw)
    second = PackManifest.from_dict(second_raw)

    assert first.coordinate == "acme/secure-coding"
    assert first.identity == "acme/secure-coding@1.2.3-rc.1+build.7"
    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert first.sha256.startswith("sha256:") and len(first.sha256) == 71
    assert "café" in first.to_json()
    assert first.supports_framework("0.5.4")
    assert not first.supports_framework("1.0.0")
    assert first.requires_api("sdai.extension/v1")
    assert [item.coordinate for item in first.dependencies] == [
        "acme/shared-rules",
        "sdai/core-quality",
    ]


def test_manifest_unicode_is_normalized_before_canonical_hashing() -> None:
    composed = _manifest()
    decomposed = deepcopy(composed)
    decomposed["description"] = "Secure cafe\u0301 engineering Δ pack"
    decomposed["contentRoots"] = ["workflows", "skills/cafe\u0301"]
    composed["description"] = "Secure café engineering Δ pack"
    composed["contentRoots"] = ["workflows", "skills/café"]

    assert PackManifest.from_dict(composed).sha256 == PackManifest.from_dict(decomposed).sha256


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda raw: raw.update({"unknown": True}), "unsupported field"),
        (lambda raw: raw.update({"id": "Bad ID"}), "portable lowercase identifier"),
        (lambda raw: raw.update({"publisher": "ACME"}), "portable lowercase identifier"),
        (lambda raw: raw.update({"version": "1.2"}), "SDAI-PACK-003"),
        (lambda raw: raw.update({"capabilities": ["skills", "skills"]}), "duplicates"),
        (lambda raw: raw.update({"contentRoots": ["skills", "skills"]}), "duplicates"),
        (lambda raw: raw["dependencies"].append(deepcopy(raw["dependencies"][0])), "repeat"),
        (lambda raw: raw.update({"compatibility": {"framework": "*", "apis": [], "extra": 1}}), "unsupported field"),
    ],
)
def test_manifest_rejects_unknown_invalid_and_duplicate_truth(mutator, message: str) -> None:
    raw = _manifest()
    mutator(raw)
    with pytest.raises(PackManifestError, match=message):
        PackManifest.from_dict(raw)


@pytest.mark.parametrize(
    "path",
    [
        "../skills",
        "./skills",
        "skills/../rules",
        "skills//rules",
        "/absolute/skills",
        "C:/absolute/skills",
        r"skills\windows",
        "skills/\x00bad",
    ],
)
def test_manifest_content_roots_fail_closed_on_unportable_paths(path: str) -> None:
    raw = _manifest()
    raw["contentRoots"] = [path]

    with pytest.raises(PackManifestError, match="SDAI-PACK-002"):
        PackManifest.from_dict(raw)


def test_load_manifest_validates_utf8_layout_and_relative_path_input(tmp_path: Path, monkeypatch) -> None:
    pack = tmp_path / "pack"
    manifest_path = _write_layout(pack)
    monkeypatch.chdir(tmp_path)

    loaded = load_pack_manifest(Path("pack/pack.yaml"))
    loaded_with_explicit_root = load_pack_manifest(Path("pack.yaml"), pack_root=Path("pack"))

    assert loaded.identity == "acme/secure-coding@1.2.3-rc.1+build.7"
    assert loaded_with_explicit_root.sha256 == loaded.sha256
    assert manifest_path.read_text(encoding="utf-8").count("café") == 2


def test_load_manifest_rejects_missing_declared_content_root(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    path = _write_layout(pack)
    (pack / "workflows").rmdir()

    with pytest.raises(PackManifestError, match="existing directory"):
        load_pack_manifest(path)


def test_load_manifest_rejects_symlinked_content_root_that_escapes_pack(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    outside = tmp_path / "outside"
    outside.mkdir()
    path = _write_layout(pack)
    target = pack / "skills" / "café"
    target.rmdir()
    target.parent.rmdir()
    try:
        (pack / "skills").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    with pytest.raises(PackManifestError, match="symlink component"):
        load_pack_manifest(path)


def test_load_manifest_rejects_manifest_symlink(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    path = _write_layout(pack)
    link = pack / "linked-pack.yaml"
    try:
        link.symlink_to(path.name)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    with pytest.raises(PackManifestError, match="manifest must not be a symlink"):
        load_pack_manifest(link)
