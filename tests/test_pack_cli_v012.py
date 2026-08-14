from __future__ import annotations

import json
from pathlib import Path

import yaml

from sdai.entrypoint import main
from sdai.pack_catalog import PackCatalog, PackCatalogEntry
from sdai.pack_integrity import build_pack_content_index
from sdai.pack_lock import PackLock, PackLockEntry
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, load_pack_manifest


def _project(root: Path) -> None:
    (root / ".sdai").mkdir(parents=True)
    (root / ".sdai" / "config.yaml").write_text("provider: mock\n", encoding="utf-8")


def _artifact(root: Path):
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "review.md").write_text("Review requirements.\n", encoding="utf-8")
    raw = {
        "apiVersion": PACK_MANIFEST_API_VERSION,
        "id": "secure-coding",
        "publisher": "acme",
        "version": "1.2.3",
        "description": "Secure coding Pack",
        "capabilities": ["skills"],
        "contentRoots": ["skills"],
        "dependencies": [],
        "compatibility": {
            "framework": ">=0.5.4,<1.0.0",
            "apis": ["sdai.pack-manifest/v1"],
        },
    }
    (root / "pack.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False), encoding="utf-8", newline="\n"
    )
    manifest = load_pack_manifest(root / "pack.yaml")
    content = build_pack_content_index(root, manifest)
    lock = PackLock(
        roots=(manifest.identity,),
        packages=(
            PackLockEntry(
                publisher=manifest.publisher,
                id=manifest.id,
                version=manifest.version,
                source="https://packs.example.test/acme/secure-coding/1.2.3",
                manifest_sha256=manifest.sha256,
                content_sha256=content.sha256,
                dependencies=(),
            ),
        ),
    )
    catalog = PackCatalog.create(
        id="test",
        source="catalog://test",
        entries=(
            PackCatalogEntry(
                manifest=manifest,
                source="https://packs.example.test/acme/secure-coding/1.2.3",
                content_sha256=content.sha256,
            ),
        ),
    )
    return manifest, lock, catalog


def test_pack_install_outdated_remove_json_is_machine_clean(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    artifact = tmp_path / "artifact"
    _project(project)
    manifest, lock, _ = _artifact(artifact)
    lock_path = tmp_path / "pack-lock.json"
    lock_path.write_text(lock.to_text(), encoding="utf-8")

    code = main(
        [
            "pack",
            "install",
            manifest.coordinate,
            "--lock",
            str(lock_path),
            "--source",
            str(artifact),
            "--json",
            "--path",
            str(project),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert captured.err == ""
    assert payload["status"] == "ok"
    assert payload["pack"]["identity"] == manifest.identity

    code = main(
        ["pack", "outdated", "--lock", str(lock_path), "--json", "--path", str(project)]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["outdated"] == []
    assert captured.err == ""

    code = main(["pack", "remove", manifest.coordinate, "--json", "--path", str(project)])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["coordinate"] == manifest.coordinate
    assert captured.err == ""


def test_pack_outdated_returns_stable_exit_two(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    artifact = tmp_path / "artifact"
    newer_artifact = tmp_path / "newer"
    _project(project)
    manifest, lock, _ = _artifact(artifact)
    lock_path = tmp_path / "pack-lock.json"
    lock_path.write_text(lock.to_text(), encoding="utf-8")
    assert main([
        "pack", "install", manifest.coordinate, "--lock", str(lock_path),
        "--source", str(artifact), "--path", str(project)
    ]) == 0
    capsys.readouterr()

    newer_manifest, newer_lock, _ = _artifact(newer_artifact)
    # Replace exact identity while retaining the same coordinate.
    raw = yaml.safe_load((newer_artifact / "pack.yaml").read_text(encoding="utf-8"))
    raw["version"] = "1.3.0"
    (newer_artifact / "pack.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    newer_manifest = load_pack_manifest(newer_artifact / "pack.yaml")
    newer_content = build_pack_content_index(newer_artifact, newer_manifest)
    newer_lock = PackLock(
        roots=(newer_manifest.identity,),
        packages=(PackLockEntry(
            publisher=newer_manifest.publisher, id=newer_manifest.id, version=newer_manifest.version,
            source="https://packs.example.test/acme/secure-coding/1.3.0",
            manifest_sha256=newer_manifest.sha256, content_sha256=newer_content.sha256, dependencies=()
        ),),
    )
    newer_lock_path = tmp_path / "newer-lock.json"
    newer_lock_path.write_text(newer_lock.to_text(), encoding="utf-8")

    code = main([
        "pack", "outdated", "--lock", str(newer_lock_path), "--json", "--path", str(project)
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert len(json.loads(captured.out)["outdated"]) == 1
    assert captured.err == ""


def test_pack_search_and_info_json(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    artifact = tmp_path / "artifact"
    _project(project)
    manifest, _, catalog = _artifact(artifact)
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(catalog.to_json() + "\n", encoding="utf-8")

    code = main([
        "pack", "search", "secure", "--catalog", str(catalog_path), "--json", "--path", str(project)
    ])
    captured = capsys.readouterr()
    assert code == 0
    search_payload = json.loads(captured.out)
    assert [item["identity"] for item in search_payload["results"]] == [manifest.identity]
    assert captured.err == ""

    code = main([
        "pack", "info", manifest.coordinate, "--catalog", str(catalog_path), "--json", "--path", str(project)
    ])
    captured = capsys.readouterr()
    assert code == 0
    info_payload = json.loads(captured.out)
    assert info_payload["results"][0]["identity"] == manifest.identity
    assert captured.err == ""

    code = main([
        "pack", "info", "acme/missing", "--catalog", str(catalog_path), "--json", "--path", str(project)
    ])
    captured = capsys.readouterr()
    assert code == 3
    assert json.loads(captured.out)["results"] == []
    assert captured.err == ""
