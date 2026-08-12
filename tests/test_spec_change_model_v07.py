from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from sdai.path_safety import PathSafetyError
from sdai.spec_changes import (
    DeltaOperationKind,
    SpecChangeError,
    change_dir,
    current_spec_path,
    load_change_metadata,
    load_current_spec,
    load_delta_document,
    load_spec_change,
    validate_change_feature_id,
    validate_domain_id,
    validate_requirement_id,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _write_current(root: Path, domain: str, body: str = "# Signing\n\n- FR-001: Sign café payloads.\n") -> Path:
    path = root / "specs" / "current" / domain / "specification.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def _write_change(
    root: Path,
    feature: str = "SIGN-123",
    *,
    domains: list[str] | None = None,
    baselines: dict[str, str | None] | None = None,
    extra: dict[str, object] | None = None,
) -> Path:
    selected_domains = domains or ["signing"]
    selected_baselines = baselines or {domain: HASH_A for domain in selected_domains}
    payload: dict[str, object] = {
        "version": 1,
        "feature_id": feature,
        "title": "Governed signing change",
        "description": "Update signing behavior without bypassing canonical truth.",
        "status": "draft",
        "domains": selected_domains,
        "baselines": selected_baselines,
    }
    if extra:
        payload.update(extra)
    path = root / "specs" / "changes" / feature / "change.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _write_delta(
    root: Path,
    *,
    feature: str = "SIGN-123",
    domain: str = "signing",
    baseline: str | None = HASH_A,
    operations: list[dict[str, object]] | None = None,
    filename: str | None = None,
    extra: dict[str, object] | None = None,
) -> Path:
    payload: dict[str, object] = {
        "version": 1,
        "domain": domain,
        "baseline_spec_sha256": baseline,
        "operations": operations
        or [
            {
                "op": "ADDED",
                "requirement_id": "FR-004",
                "definition": "The service MUST preserve café/Δ metadata.",
                "reason": "Preserve UTF-8 behavior.",
            }
        ],
    }
    if extra:
        payload.update(extra)
    path = (
        root
        / "specs"
        / "changes"
        / feature
        / "deltas"
        / (filename or f"{domain}.yaml")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def test_current_spec_model_uses_portable_source_normalized_utf8_hash_and_stable_json(
    tmp_path: Path,
) -> None:
    _write_current(tmp_path, "signing", "# Signing\r\n\r\n- FR-001: café Δ\r\n")

    current = load_current_spec(tmp_path, "signing")
    payload = json.loads(current.to_json())

    assert current.domain == "signing"
    assert current.content == "# Signing\n\n- FR-001: café Δ\n"
    assert current.sha256.startswith("sha256:") and len(current.sha256) == 71
    assert current.source == "specs/current/signing/specification.md"
    assert payload["content"].endswith("café Δ\n")
    assert "\\" not in current.source


def test_change_and_all_typed_delta_operations_parse_to_deterministic_bundle(tmp_path: Path) -> None:
    _write_change(tmp_path, domains=["signing"], baselines={"signing": HASH_A})
    _write_delta(
        tmp_path,
        operations=[
            {
                "op": "ADDED",
                "requirement_id": "FR-004",
                "definition": "Add explicit timestamp validation.",
                "reason": "New requirement.",
            },
            {
                "op": "MODIFIED",
                "requirement_id": "FR-001",
                "previous_hash": HASH_B,
                "definition": "Sign PowerShell input and preserve UTF-8 metadata.",
                "reason": "Clarify behavior.",
            },
            {
                "op": "REMOVED",
                "requirement_id": "FR-002",
                "previous_hash": HASH_C,
                "reason": "Superseded behavior.",
            },
            {
                "op": "RENAMED",
                "requirement_id": "FR-003",
                "new_requirement_id": "FR-005",
                "previous_hash": HASH_A,
                "reason": "Align requirement taxonomy.",
            },
        ],
    )

    bundle = load_spec_change(tmp_path, "SIGN-123")
    payload = bundle.as_dict()
    serialized = bundle.to_json()

    assert bundle.metadata.domains == ("signing",)
    assert bundle.metadata.baselines == {"signing": HASH_A}
    assert [operation.op for operation in bundle.deltas[0].operations] == [
        DeltaOperationKind.ADDED,
        DeltaOperationKind.MODIFIED,
        DeltaOperationKind.REMOVED,
        DeltaOperationKind.RENAMED,
    ]
    assert payload["deltas"][0]["operations"][3]["new_requirement_id"] == "FR-005"  # type: ignore[index]
    assert serialized == bundle.to_json()
    assert "specs/changes/SIGN-123/change.yaml" in serialized
    assert "specs/changes/SIGN-123/deltas/signing.yaml" in serialized
    assert "\\" not in serialized


def test_change_metadata_sorts_domains_and_requires_exact_baseline_set(tmp_path: Path) -> None:
    _write_change(
        tmp_path,
        domains=["zeta", "signing"],
        baselines={"zeta": None, "signing": HASH_A},
    )

    metadata = load_change_metadata(tmp_path, "SIGN-123")

    assert metadata.domains == ("signing", "zeta")
    assert list(metadata.as_dict()["baselines"]) == ["signing", "zeta"]

    _write_change(
        tmp_path,
        domains=["signing", "certificates"],
        baselines={"signing": HASH_A},
    )
    with pytest.raises(SpecChangeError, match="SDAI-SPEC-003.*baselines"):
        load_change_metadata(tmp_path, "SIGN-123")


@pytest.mark.parametrize("op", ["MODIFIED", "REMOVED", "RENAMED"])
def test_prior_truth_operations_require_previous_requirement_hash(tmp_path: Path, op: str) -> None:
    operation: dict[str, object] = {
        "op": op,
        "requirement_id": "FR-001",
        "reason": "Requires baseline evidence.",
    }
    if op == "MODIFIED":
        operation["definition"] = "Changed definition."
    if op == "RENAMED":
        operation["new_requirement_id"] = "FR-002"
    path = _write_delta(tmp_path, operations=[operation])

    with pytest.raises(SpecChangeError, match="SDAI-SPEC-005.*previous_hash"):
        load_delta_document(tmp_path, path)


def test_operation_specific_fields_are_strict_and_unknown_fields_fail(tmp_path: Path) -> None:
    path = _write_delta(
        tmp_path,
        operations=[
            {
                "op": "ADDED",
                "requirement_id": "FR-004",
                "previous_hash": HASH_A,
                "definition": "New definition.",
                "reason": "Invalid extra baseline evidence.",
            }
        ],
    )
    with pytest.raises(SpecChangeError, match="SDAI-SPEC-004.*forbids.*previous_hash"):
        load_delta_document(tmp_path, path)

    path = _write_delta(
        tmp_path,
        operations=[
            {
                "op": "ADDED",
                "requirement_id": "FR-004",
                "definition": "New definition.",
                "reason": "Unknown field test.",
                "typo_field": True,
            }
        ],
    )
    with pytest.raises(SpecChangeError, match="SDAI-SPEC-004.*unknown field.*typo_field"):
        load_delta_document(tmp_path, path)


def test_duplicate_target_operations_fail_as_ambiguous(tmp_path: Path) -> None:
    path = _write_delta(
        tmp_path,
        operations=[
            {
                "op": "MODIFIED",
                "requirement_id": "FR-001",
                "previous_hash": HASH_A,
                "definition": "First definition.",
                "reason": "First change.",
            },
            {
                "op": "REMOVED",
                "requirement_id": "FR-001",
                "previous_hash": HASH_A,
                "reason": "Conflicting change.",
            },
        ],
    )

    with pytest.raises(SpecChangeError, match="SDAI-SPEC-006.*FR-001"):
        load_delta_document(tmp_path, path)


def test_bundle_requires_declared_domains_exactly_once_and_matching_baselines(tmp_path: Path) -> None:
    _write_change(
        tmp_path,
        domains=["signing", "certificates"],
        baselines={"signing": HASH_A, "certificates": None},
    )
    _write_delta(tmp_path, domain="signing", baseline=HASH_B)
    _write_delta(tmp_path, domain="certificates", baseline=None)

    with pytest.raises(SpecChangeError, match="SDAI-SPEC-007.*baseline"):
        load_spec_change(tmp_path, "SIGN-123")

    _write_delta(tmp_path, domain="signing", baseline=HASH_A)
    extra = _write_delta(
        tmp_path,
        domain="signing",
        baseline=HASH_A,
        filename="signing-copy.yaml",
    )
    assert extra.exists()
    with pytest.raises(SpecChangeError, match="SDAI-SPEC-007.*more than one"):
        load_spec_change(tmp_path, "SIGN-123")


def test_bundle_rejects_undeclared_delta_domain(tmp_path: Path) -> None:
    _write_change(tmp_path, domains=["signing"], baselines={"signing": HASH_A})
    _write_delta(tmp_path, domain="certificates", baseline=None)

    with pytest.raises(SpecChangeError, match="SDAI-SPEC-007.*not declared"):
        load_spec_change(tmp_path, "SIGN-123")


def test_safe_identifier_grammars_reject_traversal_and_ambiguous_names() -> None:
    for value in ("../signing", "signing/child", "signing..old", "-signing", "signing-"):
        with pytest.raises(SpecChangeError, match="SDAI-SPEC-001"):
            validate_domain_id(value)
    for value in ("../SIGN-1", "SIGN/1", "SIGN..1", "SIGN-1-"):
        with pytest.raises(SpecChangeError, match="SDAI-SPEC-001"):
            validate_change_feature_id(value)
    for value in ("../FR-001", "FR/001", "FR..001", "FR-001-"):
        with pytest.raises(SpecChangeError, match="SDAI-SPEC-001"):
            validate_requirement_id(value)


def test_path_helpers_do_not_create_or_write_canonical_truth(tmp_path: Path) -> None:
    current = current_spec_path(tmp_path, "signing")
    change = change_dir(tmp_path, "SIGN-123")

    assert current == tmp_path.resolve() / "specs" / "current" / "signing" / "specification.md"
    assert change == tmp_path.resolve() / "specs" / "changes" / "SIGN-123"
    assert not current.exists()
    assert not change.exists()


def test_current_spec_symlink_escape_is_rejected_by_shared_path_safety(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-spec"
    outside.mkdir(exist_ok=True)
    (outside / "specification.md").write_text("# outside\n", encoding="utf-8")
    link = tmp_path / "specs" / "current" / "signing"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available on this runner")

    with pytest.raises(PathSafetyError, match="must stay inside the project workspace"):
        load_current_spec(tmp_path, "signing")
