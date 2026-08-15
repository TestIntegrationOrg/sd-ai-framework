from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from sdai.spec_changes import SpecChangeError, _read_text, load_spec_change
from sdai.specification_store_references import (
    SpecificationStoreReferenceError,
    load_specification_store_references,
)


def _digest(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def _write_change(project: Path) -> tuple[Path, Path]:
    change_root = project / "specs" / "changes" / "Feature-1"
    delta_root = change_root / "deltas"
    delta_root.mkdir(parents=True)
    metadata = change_root / "change.yaml"
    metadata.write_text(
        """version: 1
feature_id: Feature-1
title: Snapshot-bound change
status: draft
domains:
  - core
baselines:
  core: null
""",
        encoding="utf-8",
    )
    delta = delta_root / "core.yaml"
    delta.write_text(
        """version: 1
domain: core
baseline_spec_sha256: null
operations:
  - op: ADDED
    requirement_id: REQ-1
    reason: Add the first requirement
    definition: The system shall preserve snapshot identity.
""",
        encoding="utf-8",
    )
    return metadata, delta


def test_store_bound_reader_avoids_unbounded_path_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "specification.md"
    source.write_text("# specification\n", encoding="utf-8")
    expected = _digest(source)

    def fail_read_bytes(self: Path) -> bytes:
        raise AssertionError(f"unbounded read_bytes called for {self}")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    assert _read_text(
        tmp_path,
        source,
        "bound specification",
        expected_file_sha256=expected,
    ) == "# specification\n"


def test_store_bound_reader_rejects_oversized_replacement_before_digest_acceptance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "specification.md"
    source.write_bytes(b"x" * (16 * 1024 * 1024 + 1))

    with pytest.raises(SpecChangeError, match="16 MiB bound"):
        _read_text(
            tmp_path,
            source,
            "bound specification",
            expected_file_sha256="sha256:" + "0" * 64,
        )


def test_change_loader_requires_exact_bound_delta_source_set(tmp_path: Path) -> None:
    metadata, delta = _write_change(tmp_path)
    digest_map = {
        metadata.relative_to(tmp_path).as_posix(): _digest(metadata),
        delta.relative_to(tmp_path).as_posix(): _digest(delta),
    }
    actual_delta = delta.relative_to(tmp_path).as_posix()
    hidden_delta = "specs/changes/Feature-1/deltas/hidden.yaml"

    with pytest.raises(SpecChangeError, match="delta source set does not match"):
        load_spec_change(
            tmp_path,
            "Feature-1",
            expected_file_sha256_by_source=digest_map,
            expected_delta_sources=(actual_delta, hidden_delta),
        )

    bundle = load_spec_change(
        tmp_path,
        "Feature-1",
        expected_file_sha256_by_source=digest_map,
        expected_delta_sources=(actual_delta,),
    )
    assert tuple(delta_doc.source for delta_doc in bundle.deltas) == (actual_delta,)


def test_custom_reference_declaration_cannot_escape_project_with_parent_segments(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("not: relevant\n", encoding="utf-8")

    with pytest.raises(SpecificationStoreReferenceError, match="stay inside the project"):
        load_specification_store_references(project, Path("../outside.yaml"))
