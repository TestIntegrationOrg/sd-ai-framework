from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.specification_store_lifecycle import (
    StoreAutomationExit,
    StoreLifecycleError,
    create_store,
    doctor_stores,
    export_store_context,
    list_stores,
    register_store,
)
from sdai.specification_store_references import load_specification_store_references


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".sdai").mkdir(parents=True)
    (root / ".sdai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return root


def test_create_store_is_idempotent_and_rejects_unmanaged_destination(tmp_path: Path) -> None:
    destination = tmp_path / "store"
    first = create_store(destination, "platform-specs", "1.2.3", description="Platform specifications")
    second = create_store(destination, "platform-specs", "1.2.3", description="Platform specifications")

    assert first.created is True
    assert second.created is False
    assert first.identity == second.identity == "platform-specs@1.2.3"
    assert first.manifest_sha256 == second.manifest_sha256
    assert (destination / ".sdai-store" / "store.yaml").is_file()
    assert (destination / "specs" / "current").is_dir()
    assert (destination / "specs" / "changes").is_dir()

    unmanaged = tmp_path / "unmanaged"
    unmanaged.mkdir()
    (unmanaged / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(StoreLifecycleError, match="unmanaged destination"):
        create_store(unmanaged, "platform-specs", "1.2.3")
    assert (unmanaged / "keep.txt").read_text(encoding="utf-8") == "do not overwrite"


def test_register_store_is_idempotent_and_never_mutates_store_content(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = tmp_path / "external-store"
    create_store(store, "platform-specs", "1.0.0")
    specification = store / "specs" / "current" / "core" / "specification.md"
    specification.parent.mkdir(parents=True)
    specification.write_text("# Core\n", encoding="utf-8")
    before = specification.read_bytes()

    first = register_store(project, store)
    second = register_store(project, store)

    assert first.registered is True
    assert second.registered is False
    assert first.path_scope == second.path_scope == "external"
    assert specification.read_bytes() == before
    references = load_specification_store_references(project)
    assert len(references.references) == 1
    assert references.references[0].identity == "platform-specs@1.0.0"


def test_register_rejects_same_identity_at_a_different_path(tmp_path: Path) -> None:
    project = _project(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    create_store(first, "platform-specs", "1.0.0")
    create_store(second, "platform-specs", "1.0.0")
    register_store(project, first)

    with pytest.raises(StoreLifecycleError, match="different explicit path"):
        register_store(project, second)


def test_list_and_context_are_canonical_and_redact_local_paths(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = tmp_path / "private" / "platform-store"
    create_store(store, "platform-specs", "1.0.0")
    register_store(project, store)

    listing = list_stores(project)
    context = export_store_context(project)
    listing_json = listing.to_json()
    context_json = context.to_json()

    assert json.dumps(json.loads(listing_json), sort_keys=True, separators=(",", ":"), ensure_ascii=False) == listing_json
    assert json.dumps(json.loads(context_json), sort_keys=True, separators=(",", ":"), ensure_ascii=False) == context_json
    assert str(store.resolve()) not in listing_json
    assert str(store.resolve()) not in context_json
    assert listing.stores[0].path_scope == "external"
    assert context.stores[0].identity == "platform-specs@1.0.0"
    assert context.stores[0].capabilities == ("changes", "current-specifications")


def test_context_can_select_an_exact_registered_store(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = tmp_path / "store"
    create_store(store, "platform-specs", "1.0.0")
    register_store(project, store)

    context = export_store_context(project, store="platform-specs", version="1.0.0")
    assert tuple(item.identity for item in context.stores) == ("platform-specs@1.0.0",)

    with pytest.raises(StoreLifecycleError, match="not registered"):
        export_store_context(project, store="missing", version="1.0.0")


def test_doctor_has_stable_exit_classes_and_empty_project_is_healthy(tmp_path: Path) -> None:
    project = _project(tmp_path)
    empty = doctor_stores(project)
    assert empty.healthy is True
    assert empty.exit_code is StoreAutomationExit.SUCCESS
    assert empty.findings[0].code == "SDAI-STORE-DOCTOR-001"

    declaration = project / ".sdai" / "specification-stores.yaml"
    declaration.write_text("not: a-valid-reference-contract\n", encoding="utf-8")
    unhealthy = doctor_stores(project)
    assert unhealthy.healthy is False
    assert unhealthy.exit_code is StoreAutomationExit.UNHEALTHY
    assert unhealthy.findings[0].code == "SDAI-STORE-DOCTOR-002"
    assert "SpecificationStore references" in unhealthy.findings[0].message
