from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

import sdai.pack_integrity as pack_integrity
from sdai.pack_integrity import PackContentEntry, PackIntegrityError, build_pack_content_index
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, PackManifest


def _manifest(*roots: str) -> PackManifest:
    return PackManifest.from_dict(
        {
            "apiVersion": PACK_MANIFEST_API_VERSION,
            "id": "secure-coding",
            "publisher": "acme",
            "version": "1.0.0",
            "description": "Path safety regression",
            "capabilities": ["skills"],
            "contentRoots": list(roots or ("skills",)),
            "dependencies": [],
            "compatibility": {"framework": ">=0.5.4,<1.0.0", "apis": []},
        }
    )


def test_nested_symlinked_file_is_rejected(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = skills / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    with pytest.raises(PackIntegrityError, match="must not be a symlink"):
        build_pack_content_index(tmp_path, _manifest())


def test_nested_symlinked_directory_is_rejected(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "rule.md").write_text("outside", encoding="utf-8")
    link = skills / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    with pytest.raises(PackIntegrityError, match="must not be a symlink"):
        build_pack_content_index(tmp_path, _manifest())


def test_case_insensitive_path_collision_fails_closed_when_filesystem_allows_both(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    upper = skills / "Rule.md"
    lower = skills / "rule.md"
    upper.write_text("upper", encoding="utf-8")
    lower.write_text("lower", encoding="utf-8")
    if upper.samefile(lower):
        pytest.skip("filesystem is case-insensitive and cannot represent both collision inputs")

    with pytest.raises(PackIntegrityError, match="case-insensitive path collisions"):
        build_pack_content_index(tmp_path, _manifest())


def test_walk_scan_error_fails_closed_instead_of_omitting_subtree(tmp_path: Path, monkeypatch) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "visible.md").write_text("visible", encoding="utf-8")

    def failing_walk(*args, onerror=None, **kwargs):
        assert onerror is not None
        onerror(PermissionError(13, "permission denied", str(skills / "private")))
        return iter(())

    monkeypatch.setattr(pack_integrity.os, "walk", failing_walk)

    with pytest.raises(PackIntegrityError, match="unable to completely scan Pack content.*private"):
        build_pack_content_index(tmp_path, _manifest())


@pytest.mark.parametrize(
    "path",
    [
        "C:/skills/a.md",
        "/skills/a.md",
        "skills/../a.md",
        r"skills\a.md",
        "skills/CON",
        "skills/con.txt",
        "skills/PRN.md",
        "skills/AUX",
        "skills/NUL.json",
        "skills/COM1.txt",
        "skills/LPT9",
        "skills/rule.",
        "skills/rule ",
        "skills/a:b.md",
        "skills/a?.md",
        "skills/a*.md",
        "skills/a|b.md",
        "skills/a\x1fb.md",
    ],
)
def test_direct_content_entry_rejects_unportable_paths(path: str) -> None:
    with pytest.raises(PackIntegrityError, match="SDAI-PACK-INTEGRITY-002"):
        PackContentEntry(
            path=path,
            sha256="sha256:" + sha256(b"x").hexdigest(),
            size=1,
        )
