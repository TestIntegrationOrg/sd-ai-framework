from __future__ import annotations

from pathlib import Path

import pytest

from sdai.pack_manifest import PackManifestError, load_pack_manifest


def test_pack_manifest_yaml_duplicate_keys_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    manifest = tmp_path / "pack.yaml"
    manifest.write_text(
        """apiVersion: sdai.pack-manifest/v1
id: secure-coding
id: replaced-value
publisher: acme
version: 1.0.0
description: duplicate key must not silently win
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

    with pytest.raises(PackManifestError, match="SDAI-PACK-001.*unable to read"):
        load_pack_manifest(manifest)
