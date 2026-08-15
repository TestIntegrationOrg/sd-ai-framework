from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest
import yaml

from sdai.specification_store_references import (
    SPECIFICATION_STORE_CONTENT_SNAPSHOT_API_VERSION,
    SPECIFICATION_STORE_REFERENCES_API_VERSION,
    SPECIFICATION_STORE_REFERENCES_MAX_BYTES,
    SpecificationStoreContentEntry,
    SpecificationStoreContentSnapshot,
    SpecificationStoreReferenceError,
    load_specification_store_references,
    resolve_specification_store_references,
)
from sdai.specification_stores import (
    SPECIFICATION_STORE_MANIFEST_API_VERSION,
    SpecificationStoreLayer,
    SpecificationStoreSource,
    build_specification_store_registry,
)


HASH_A = "sha256:" + "a" * 64


def _write_store(
    root: Path,
    *,
    store_id: str = "platform-specs",
    version: str = "1.0.0",
    description: str = "Platform specifications café Δ",
) -> None:
    roots = {
        "changes": "knowledge/changes",
        "current": "knowledge/current",
    }
    for relative in roots.values():
        (root / relative).mkdir(parents=True, exist_ok=True)
    manifest = root / ".sdai-store" / "store.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        yaml.safe_dump(
            {
                "apiVersion": SPECIFICATION_STORE_MANIFEST_API_VERSION,
                "kind": "SpecificationStore",
                "metadata": {
                    "id": store_id,
                    "version": version,
                    "description": description,
                },
                "spec": {
                    "specificationRoots": roots,
                    "capabilities": ["changes", "current-specifications"],
                    "metadata": {"owner": "platform-architecture"},
                },
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
        newline="\n",
    )
    current = root / roots["current"] / "signing" / "specification.md"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(
        "# Signing\n\n- FR-001: Preserve café Δ metadata.\n",
        encoding="utf-8",
        newline="\n",
    )
    change_root = root / roots["changes"] / "SIGN-123"
    change_root.mkdir(parents=True, exist_ok=True)
    (change_root / "change.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "feature_id": "SIGN-123",
                "title": "Governed signing change",
                "description": "Preserve referenced truth.",
                "status": "draft",
                "domains": ["signing"],
                "baselines": {"signing": HASH_A},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    deltas = change_root / "deltas"
    deltas.mkdir(exist_ok=True)
    (deltas / "signing.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "domain": "signing",
                "baseline_spec_sha256": HASH_A,
                "operations": [
                    {
                        "op": "ADDED",
                        "requirement_id": "FR-002",
                        "definition": "The service MUST preserve provenance.",
                        "reason": "Cross-store traceability.",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )


def _write_references(
    project: Path,
    references: list[dict[str, object]],
) -> Path:
    path = project / ".sdai" / "specification-stores.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": SPECIFICATION_STORE_REFERENCES_API_VERSION,
                "kind": "SpecificationStoreReferences",
                "references": references,
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _reference(store: Path, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "store": "platform-specs",
        "version": "1.0.0",
        "path": str(store),
    }
    payload.update(extra)
    return payload


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    store = tmp_path / "store"
    project.mkdir()
    _write_store(store)
    _write_references(project, [_reference(store)])
    return project, store


def _bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_reference_resolution_is_read_only_canonical_and_content_complete(
    tmp_path: Path,
) -> None:
    project, store = _workspace(tmp_path)
    project_before = _bytes(project)
    store_before = _bytes(store)

    first = resolve_specification_store_references(project)
    second = resolve_specification_store_references(project)
    selected = first.get("platform-specs", "1.0.0")

    assert selected is not None
    assert selected.identity == "platform-specs@1.0.0"
    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert selected.snapshot.as_dict()["apiVersion"] == SPECIFICATION_STORE_CONTENT_SNAPSHOT_API_VERSION
    assert [entry.path for entry in selected.snapshot.entries] == [
        "knowledge/changes/SIGN-123/change.yaml",
        "knowledge/changes/SIGN-123/deltas/signing.yaml",
        "knowledge/current/signing/specification.md",
    ]
    assert selected.snapshot.sha256.startswith("sha256:")
    assert _bytes(project) == project_before
    assert _bytes(store) == store_before


def test_exact_content_binding_accepts_current_and_rejects_dirty_store(tmp_path: Path) -> None:
    project, store = _workspace(tmp_path)
    resolved = resolve_specification_store_references(project).references[0]
    _write_references(
        project,
        [
            _reference(
                store,
                content={
                    "manifestSha256": resolved.manifest.sha256,
                    "snapshotSha256": resolved.snapshot.sha256,
                },
            )
        ],
    )

    rebound = resolve_specification_store_references(project).references[0]
    assert rebound.snapshot.sha256 == resolved.snapshot.sha256

    target = store / "knowledge" / "current" / "signing" / "specification.md"
    target.write_text("# Dirty truth\n", encoding="utf-8", newline="\n")
    with pytest.raises(SpecificationStoreReferenceError, match="content binding is stale"):
        resolve_specification_store_references(project)


def test_current_and_change_reads_preserve_store_manifest_and_content_provenance(
    tmp_path: Path,
) -> None:
    project, _ = _workspace(tmp_path)
    selected = resolve_specification_store_references(project).references[0]

    current = selected.read_current("signing")
    change = selected.read_change("SIGN-123")

    assert current.specification.source == "knowledge/current/signing/specification.md"
    assert current.provenance.store_identity == "platform-specs@1.0.0"
    assert current.provenance.manifest_sha256 == selected.manifest.sha256
    assert current.provenance.snapshot_sha256 == selected.snapshot.sha256
    assert [item.path for item in current.provenance.content] == [
        "knowledge/current/signing/specification.md"
    ]
    assert change.change.metadata.source == "knowledge/changes/SIGN-123/change.yaml"
    assert [item.path for item in change.provenance.content] == [
        "knowledge/changes/SIGN-123/change.yaml",
        "knowledge/changes/SIGN-123/deltas/signing.yaml",
    ]


def test_read_fails_when_store_changes_after_resolution(tmp_path: Path) -> None:
    project, store = _workspace(tmp_path)
    selected = resolve_specification_store_references(project).references[0]
    (store / "knowledge" / "current" / "signing" / "specification.md").write_text(
        "# Changed after resolution\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(SpecificationStoreReferenceError, match="dirty or stale"):
        selected.read_current("signing")


def test_mutation_between_snapshot_passes_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, store = _workspace(tmp_path)
    import sdai.specification_store_references as references_module

    original = references_module._build_content_snapshot_once
    calls = 0

    def mutate_after_first(root: Path, manifest):  # type: ignore[no-untyped-def]
        nonlocal calls
        snapshot = original(root, manifest)
        calls += 1
        if calls == 1:
            (store / "knowledge" / "current" / "signing" / "specification.md").write_text(
                "# Mutation during inspection\n",
                encoding="utf-8",
                newline="\n",
            )
        return snapshot

    monkeypatch.setattr(references_module, "_build_content_snapshot_once", mutate_after_first)
    with pytest.raises(SpecificationStoreReferenceError, match="mutated during read-only inspection"):
        resolve_specification_store_references(project)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"path": "missing-store"}, "existing local directory"),
        ({"store": "other-store"}, "does not match the manifest"),
        ({"version": "2.0.0"}, "does not match the manifest"),
    ],
)
def test_missing_or_mismatched_reference_fails_closed(
    tmp_path: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    project, store = _workspace(tmp_path)
    declared = _reference(store)
    declared.update(mutation)
    _write_references(project, [declared])
    with pytest.raises(SpecificationStoreReferenceError, match=message):
        resolve_specification_store_references(project)


def test_registry_manifest_mismatch_is_stale(tmp_path: Path) -> None:
    project, store = _workspace(tmp_path)
    registry_store = tmp_path / "registry-store"
    _write_store(registry_store, description="Different canonical manifest")
    registry = build_specification_store_registry(
        (
            SpecificationStoreSource(
                registry_store,
                SpecificationStoreLayer.REPO,
                "repository",
            ),
        )
    )
    with pytest.raises(SpecificationStoreReferenceError, match="stale in the store registry"):
        resolve_specification_store_references(project, registry)


def test_duplicate_identity_and_overlapping_resolved_paths_fail_closed(tmp_path: Path) -> None:
    project, store = _workspace(tmp_path)
    _write_references(project, [_reference(store), _reference(store, path=str(store / "."))])
    with pytest.raises(SpecificationStoreReferenceError, match="duplicate exact store identity"):
        load_specification_store_references(project)

    nested = store / "nested-store"
    _write_store(nested, store_id="nested-specs")
    _write_references(
        project,
        [
            _reference(store),
            {
                "store": "nested-specs",
                "version": "1.0.0",
                "path": str(nested),
            },
        ],
    )
    with pytest.raises(SpecificationStoreReferenceError, match="must not duplicate or overlap"):
        resolve_specification_store_references(project)


def test_case_colliding_content_paths_fail_closed() -> None:
    with pytest.raises(SpecificationStoreReferenceError, match="case-insensitive content path"):
        SpecificationStoreContentSnapshot(
            identity="platform-specs@1.0.0",
            manifest_sha256=HASH_A,
            manifest_file_sha256=HASH_A,
            entries=(
                SpecificationStoreContentEntry(
                    root="current",
                    path="knowledge/current/Case.md",
                    sha256=HASH_A,
                    size=3,
                ),
                SpecificationStoreContentEntry(
                    root="current",
                    path="knowledge/current/case.md",
                    sha256=HASH_A,
                    size=3,
                ),
            ),
        )


def test_symlinked_store_or_content_is_rejected(tmp_path: Path) -> None:
    project, store = _workspace(tmp_path)
    alias = tmp_path / "store-alias"
    try:
        alias.symlink_to(store, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    _write_references(project, [_reference(alias)])
    with pytest.raises(SpecificationStoreReferenceError, match="symlink, junction, or reparse"):
        resolve_specification_store_references(project)

    _write_references(project, [_reference(store)])
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    link = store / "knowledge" / "current" / "redirect.md"
    link.symlink_to(outside)
    with pytest.raises(SpecificationStoreReferenceError, match="content files.*redirect"):
        resolve_specification_store_references(project)


def test_reference_declaration_is_strict_bounded_utf8_without_aliases(tmp_path: Path) -> None:
    project, store = _workspace(tmp_path)
    path = project / ".sdai" / "specification-stores.yaml"
    path.write_text(
        "apiVersion: sdai.specification-store-references/v1\n"
        "kind: SpecificationStoreReferences\n"
        "kind: Duplicate\n"
        "references: []\n",
        encoding="utf-8",
    )
    with pytest.raises(SpecificationStoreReferenceError, match="YAML is malformed"):
        load_specification_store_references(project)

    path.write_text(
        "apiVersion: sdai.specification-store-references/v1\n"
        "kind: SpecificationStoreReferences\n"
        f"references: &refs\n  - store: platform-specs\n    version: 1.0.0\n    path: {store}\n"
        "extra: *refs\n",
        encoding="utf-8",
    )
    with pytest.raises(SpecificationStoreReferenceError, match="must not contain YAML aliases"):
        load_specification_store_references(project)

    path.write_bytes(b"apiVersion: \xff")
    with pytest.raises(SpecificationStoreReferenceError, match="not valid UTF-8"):
        load_specification_store_references(project)

    path.write_bytes(b"#" * (SPECIFICATION_STORE_REFERENCES_MAX_BYTES + 1))
    with pytest.raises(SpecificationStoreReferenceError, match="1 MiB input limit"):
        load_specification_store_references(project)


def test_snapshot_hash_binds_raw_manifest_and_content_bytes(tmp_path: Path) -> None:
    project, store = _workspace(tmp_path)
    first = resolve_specification_store_references(project).references[0].snapshot
    manifest = store / ".sdai-store" / "store.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "# formatting-only change\n",
        encoding="utf-8",
        newline="\n",
    )
    second = resolve_specification_store_references(project).references[0].snapshot

    assert first.manifest_sha256 == second.manifest_sha256
    assert first.manifest_file_sha256 != second.manifest_file_sha256
    assert first.sha256 != second.sha256
    assert json.loads(first.to_json())["manifestFileSha256"].startswith("sha256:")


def test_content_entry_hash_is_raw_sha256(tmp_path: Path) -> None:
    project, store = _workspace(tmp_path)
    selected = resolve_specification_store_references(project).references[0]
    entry = selected.snapshot.entry("knowledge/current/signing/specification.md")
    assert entry is not None
    expected = sha256(
        (store / "knowledge" / "current" / "signing" / "specification.md").read_bytes()
    ).hexdigest()
    assert entry.sha256 == "sha256:" + expected


@pytest.mark.parametrize(
    ("limit", "value", "message"),
    [
        ("SPECIFICATION_STORE_CONTENT_MAX_FILE_BYTES", 8, "per-file snapshot limit"),
        ("SPECIFICATION_STORE_CONTENT_MAX_TOTAL_BYTES", 8, "256 MiB snapshot limit"),
        ("SPECIFICATION_STORE_CONTENT_MAX_FILES", 2, "file snapshot limit"),
        ("SPECIFICATION_STORE_CONTENT_MAX_DIRECTORIES", 1, "directory snapshot limit"),
    ],
)
def test_content_snapshot_resource_bounds_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    value: int,
    message: str,
) -> None:
    project, _ = _workspace(tmp_path)
    import sdai.specification_store_references as references_module

    monkeypatch.setattr(references_module, limit, value)
    with pytest.raises(SpecificationStoreReferenceError, match=message):
        resolve_specification_store_references(project)


def test_invalid_store_manifest_is_reported_as_reference_failure(tmp_path: Path) -> None:
    project, store = _workspace(tmp_path)
    (store / ".sdai-store" / "store.yaml").unlink()

    with pytest.raises(
        SpecificationStoreReferenceError,
        match="does not resolve to a valid SpecificationStore",
    ):
        resolve_specification_store_references(project)
