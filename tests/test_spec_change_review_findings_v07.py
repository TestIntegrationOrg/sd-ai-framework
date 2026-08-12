from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import sdai.spec_changes as spec_changes
from sdai.spec_changes import (
    SpecChangeError,
    load_current_spec,
    load_delta_document,
    validate_change_feature_id,
    validate_domain_id,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _write_delta(root: Path, operations: list[dict[str, object]]) -> Path:
    path = root / "delta.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "domain": "signing",
                "baseline_spec_sha256": HASH_A,
                "operations": operations,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_rename_destination_collides_with_other_operation_target(tmp_path: Path) -> None:
    path = _write_delta(
        tmp_path,
        [
            {
                "op": "RENAMED",
                "requirement_id": "FR-001",
                "new_requirement_id": "FR-002",
                "previous_hash": HASH_A,
                "reason": "Rename requirement.",
            },
            {
                "op": "ADDED",
                "requirement_id": "FR-002",
                "definition": "Conflicting new definition.",
                "reason": "Would collide with rename destination.",
            },
        ],
    )

    with pytest.raises(SpecChangeError, match="SDAI-SPEC-006.*FR-002"):
        load_delta_document(tmp_path, path)


def test_two_renames_cannot_share_the_same_destination(tmp_path: Path) -> None:
    path = _write_delta(
        tmp_path,
        [
            {
                "op": "RENAMED",
                "requirement_id": "FR-001",
                "new_requirement_id": "FR-003",
                "previous_hash": HASH_A,
                "reason": "First rename.",
            },
            {
                "op": "RENAMED",
                "requirement_id": "FR-002",
                "new_requirement_id": "FR-003",
                "previous_hash": HASH_B,
                "reason": "Second rename.",
            },
        ],
    )

    with pytest.raises(SpecChangeError, match="SDAI-SPEC-006.*FR-003"):
        load_delta_document(tmp_path, path)


@pytest.mark.parametrize(
    ("operation", "forbidden"),
    [
        (
            {
                "op": "ADDED",
                "requirement_id": "FR-004",
                "definition": "New requirement.",
                "reason": "Strict field contract.",
                "previous_hash": None,
            },
            "previous_hash",
        ),
        (
            {
                "op": "REMOVED",
                "requirement_id": "FR-001",
                "previous_hash": HASH_A,
                "reason": "Strict field contract.",
                "definition": None,
            },
            "definition",
        ),
    ],
)
def test_forbidden_operation_fields_fail_even_when_yaml_value_is_null(
    tmp_path: Path,
    operation: dict[str, object],
    forbidden: str,
) -> None:
    path = _write_delta(tmp_path, [operation])

    with pytest.raises(SpecChangeError, match=rf"SDAI-SPEC-004.*forbids.*{forbidden}"):
        load_delta_document(tmp_path, path)


def test_filesystem_read_error_is_wrapped_in_structured_spec_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "specs" / "current" / "signing" / "specification.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Signing\n", encoding="utf-8")

    def fail_read(_: Path) -> str:
        raise PermissionError("simulated read denial")

    monkeypatch.setattr(spec_changes, "read_utf8_text", fail_read)

    with pytest.raises(
        SpecChangeError,
        match="SDAI-SPEC-002.*unable to read.*simulated read denial",
    ):
        load_current_spec(tmp_path, "signing")


@pytest.mark.parametrize(
    "value",
    ["con", "NUL", "COM1", "com9.txt", "LPT1", "lpt9.data", "PRN"],
)
def test_path_bearing_identifiers_reject_windows_reserved_names(value: str) -> None:
    with pytest.raises(SpecChangeError, match="SDAI-SPEC-001.*Windows-reserved"):
        validate_change_feature_id(value)

    # Domain grammar is lowercase-only; exercise reserved names in its valid case.
    with pytest.raises(SpecChangeError, match="SDAI-SPEC-001.*Windows-reserved"):
        validate_domain_id(value.lower().split(".", 1)[0])
