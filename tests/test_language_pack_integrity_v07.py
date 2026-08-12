from __future__ import annotations

from pathlib import Path
import shutil

import pytest
import yaml

from sdai.language_packs import LanguagePackError, load_language_pack


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _copy_java_pack(root: Path) -> None:
    source = _repo_root()
    for name in ("java-engineering", "spring-boot"):
        src = source / ".agents" / "skills" / name
        dst = root / ".agents" / "skills" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
    manifest = root / ".sdai" / "extensions" / "packs" / "sdai-java.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        source / ".sdai" / "extensions" / "packs" / "sdai-java.yaml",
        manifest,
    )


def test_pack_manifest_id_must_match_file_identity(tmp_path: Path) -> None:
    _copy_java_pack(tmp_path)
    path = tmp_path / ".sdai" / "extensions" / "packs" / "sdai-java.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["metadata"]["id"] = "sdai-dotnet"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        LanguagePackError,
        match="SDAI-LANGPACK-001.*sdai-dotnet.*sdai-java",
    ):
        load_language_pack(tmp_path, "sdai-java")


def test_framework_group_requires_actual_framework_compatibility(tmp_path: Path) -> None:
    _copy_java_pack(tmp_path)
    path = tmp_path / ".agents" / "skills" / "spring-boot" / "sdai.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["compatibility"] = {"languages": {"java": None}}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        LanguagePackError,
        match="SDAI-LANGPACK-003.*spring-boot.*framework compatibility",
    ):
        load_language_pack(tmp_path, "sdai-java")
