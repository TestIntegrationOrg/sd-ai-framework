from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.pack_integrity import build_pack_content_index
from sdai.pack_lifecycle import PackLifecycleError, install_from_local, operation_journal_path
from sdai.pack_lock import PackLock, PackLockEntry
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, load_pack_manifest


def test_byte_identical_unmanaged_destination_never_becomes_managed_on_retry(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    (project / ".sdai").mkdir(parents=True)
    (project / ".sdai" / "config.yaml").write_text("provider: mock\n", encoding="utf-8")
    (source / "skills").mkdir(parents=True)
    data = b"review requirements\n"
    (source / "skills" / "review.md").write_bytes(data)
    raw = {
        "apiVersion": PACK_MANIFEST_API_VERSION,
        "id": "secure-coding",
        "publisher": "acme",
        "version": "1.2.3",
        "description": "Secure coding",
        "capabilities": ["skills"],
        "contentRoots": ["skills"],
        "dependencies": [],
        "compatibility": {
            "framework": ">=0.5.4,<1.0.0",
            "apis": ["sdai.pack-manifest/v1"],
        },
    }
    (source / "pack.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    manifest = load_pack_manifest(source / "pack.yaml")
    content = build_pack_content_index(source, manifest)
    lock = PackLock(
        roots=(manifest.identity,),
        packages=(PackLockEntry(
            publisher=manifest.publisher,
            id=manifest.id,
            version=manifest.version,
            source="https://packs.example.test/acme/secure-coding/1.2.3",
            manifest_sha256=manifest.sha256,
            content_sha256=content.sha256,
            dependencies=(),
        ),),
    )

    unmanaged = project / ".sdai" / "installed-packs" / "acme" / "secure-coding" / "1.2.3" / "skills" / "review.md"
    unmanaged.parent.mkdir(parents=True)
    unmanaged.write_bytes(data)

    for _ in range(2):
        with pytest.raises(PackLifecycleError, match="refusing to overwrite unmanaged file"):
            install_from_local(project, source, lock, manifest.coordinate)
        assert unmanaged.read_bytes() == data
        assert not operation_journal_path(project).exists()
