from __future__ import annotations

from pathlib import Path

import pytest

from sdai.pack_manifest import PackManifest, PackManifestError, load_pack_manifest


def test_pack_manifest_json_duplicate_keys_fail_closed() -> None:
    payload = """{
      "apiVersion":"sdai.pack-manifest/v1",
      "id":"secure-coding",
      "id":"replaced-value",
      "publisher":"acme",
      "version":"1.0.0",
      "description":"duplicate JSON key must not silently win",
      "capabilities":["skills"],
      "contentRoots":["skills"],
      "dependencies":[],
      "compatibility":{"framework":">=0.5.4,<1.0.0","apis":[]}
    }"""

    with pytest.raises(PackManifestError, match="duplicate key 'id'"):
        PackManifest.from_json(payload)


def test_load_pack_manifest_accepts_symlinked_ancestor_above_pack_root(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real-workspace"
    pack_root = real_workspace / "pack"
    (pack_root / "skills").mkdir(parents=True)
    (pack_root / "pack.yaml").write_text(
        """apiVersion: sdai.pack-manifest/v1
id: secure-coding
publisher: acme
version: 1.0.0
description: workspace alias regression
capabilities:
  - skills
contentRoots:
  - skills
dependencies: []
compatibility:
  framework: '>=0.5.4,<1.0.0'
  apis: []
""",
        encoding="utf-8",
        newline="\n",
    )
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(real_workspace, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    loaded = load_pack_manifest(
        alias / "pack" / "pack.yaml",
        pack_root=alias / "pack",
    )

    assert loaded.identity == "acme/secure-coding@1.0.0"
