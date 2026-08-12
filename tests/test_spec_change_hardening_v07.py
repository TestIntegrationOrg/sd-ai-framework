from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from sdai.path_safety import PathSafetyError
from sdai.spec_changes import SpecChangeError, load_change_metadata, load_spec_change


HASH_A = "sha256:" + "a" * 64


def test_baseline_mapping_rejects_whitespace_alias_instead_of_leaking_key_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "specs" / "changes" / "SIGN-123" / "change.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "feature_id": "SIGN-123",
                "title": "Signing change",
                "status": "draft",
                "domains": ["signing"],
                "baselines": {" signing ": HASH_A},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        SpecChangeError,
        match="SDAI-SPEC-001.*leading or trailing whitespace",
    ):
        load_change_metadata(tmp_path, "SIGN-123")


def test_delta_symlink_cannot_escape_its_change_delta_directory(
    tmp_path: Path,
) -> None:
    change_root = tmp_path / "specs" / "changes" / "SIGN-123"
    delta_root = change_root / "deltas"
    delta_root.mkdir(parents=True, exist_ok=True)
    (change_root / "change.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "feature_id": "SIGN-123",
                "title": "Signing change",
                "status": "draft",
                "domains": ["signing"],
                "baselines": {"signing": HASH_A},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    elsewhere = tmp_path / "specs" / "other-feature-delta.yaml"
    elsewhere.parent.mkdir(parents=True, exist_ok=True)
    elsewhere.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "domain": "signing",
                "baseline_spec_sha256": HASH_A,
                "operations": [
                    {
                        "op": "ADDED",
                        "requirement_id": "FR-004",
                        "definition": "Do not import another change's delta by symlink.",
                        "reason": "Contain change-local source files.",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    link = delta_root / "signing.yaml"
    try:
        os.symlink(elsewhere, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available on this runner")

    with pytest.raises(PathSafetyError, match="must stay inside the project workspace"):
        load_spec_change(tmp_path, "SIGN-123")
