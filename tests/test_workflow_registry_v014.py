from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.extensions.registry import RegistryLayer
from sdai.workflow_registry import (
    LEGACY_WORKFLOW_REGISTRY_VERSION,
    WORKFLOW_REGISTRY_API_VERSION,
    WORKFLOW_REGISTRY_RESOLUTION_API_VERSION,
    WorkflowRegistryError,
    WorkflowSource,
    build_workflow_registry,
)


def _write_workflow(
    root: Path,
    name: str = "delivery",
    *,
    registry_version: str | None = None,
    engine_version: int = 9,
    action: str = "build",
) -> Path:
    path = root / ".sdai" / "workflows" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "name": name,
        "version": engine_version,
        "validation_mode": "standard",
        "steps": [{"id": action, "type": "deterministic", "action": action}],
    }
    if registry_version is not None:
        data["registry_version"] = registry_version
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def _source(root: Path, layer: RegistryLayer, name: str, *, locked: bool = False) -> WorkflowSource:
    return WorkflowSource(root, layer, name, locked)


def test_legacy_repository_workflow_remains_discoverable_at_zero_version(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_workflow(repo, registry_version=None)
    registry = build_workflow_registry([_source(repo, RegistryLayer.REPO, "repo")])

    resolved = registry.resolve("delivery")
    assert str(resolved.registry_version) == LEGACY_WORKFLOW_REGISTRY_VERSION
    assert resolved.identity == "delivery@0.0.0"
    assert resolved.registration.engine_version == 9
    assert resolved.selected_provenance.path == ".sdai/workflows/delivery.yaml"
    assert resolved.as_dict()["apiVersion"] == WORKFLOW_REGISTRY_RESOLUTION_API_VERSION
    assert registry.as_dict()["apiVersion"] == WORKFLOW_REGISTRY_API_VERSION


def test_latest_semver_wins_across_unlocked_layers_independent_of_source_order(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    repo = tmp_path / "repo"
    user = tmp_path / "user"
    _write_workflow(builtin, registry_version="1.0.0", action="build")
    _write_workflow(repo, registry_version="2.0.0", action="test")
    _write_workflow(user, registry_version="1.5.0", action="review")
    sources = [
        _source(user, RegistryLayer.USER, "user"),
        _source(builtin, RegistryLayer.BUILTIN, "builtin"),
        _source(repo, RegistryLayer.REPO, "repo"),
    ]

    forward = build_workflow_registry(sources)
    reverse = build_workflow_registry(list(reversed(sources)))

    assert forward.resolve("delivery").identity == "delivery@2.0.0"
    assert forward.to_json() == reverse.to_json()
    assert forward.resolve("delivery@1.5.0").selected_provenance.layer == RegistryLayer.USER


def test_exact_same_definition_preserves_all_provenance_and_selects_higher_layer(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    repo = tmp_path / "repo"
    _write_workflow(builtin, registry_version="1.0.0")
    _write_workflow(repo, registry_version="1.0.0")
    registry = build_workflow_registry(
        [_source(repo, RegistryLayer.REPO, "repo"), _source(builtin, RegistryLayer.BUILTIN, "builtin")]
    )

    resolved = registry.resolve("delivery@1.0.0")
    assert resolved.selected_provenance.layer == RegistryLayer.REPO
    assert [item.layer for item in resolved.provenance] == [RegistryLayer.BUILTIN, RegistryLayer.REPO]
    assert resolved.source_sha256.startswith("sha256:")
    assert resolved.graph_sha256.startswith("sha256:")
    assert resolved.resolution_sha256.startswith("sha256:")


def test_conflicting_exact_identity_fails_closed(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    repo = tmp_path / "repo"
    _write_workflow(builtin, registry_version="1.0.0", action="build")
    _write_workflow(repo, registry_version="1.0.0", action="different")

    with pytest.raises(WorkflowRegistryError, match="conflicting exact workflow"):
        build_workflow_registry(
            [_source(builtin, RegistryLayer.BUILTIN, "builtin"), _source(repo, RegistryLayer.REPO, "repo")]
        )


def test_authoritative_org_lock_blocks_every_higher_layer_version(tmp_path: Path) -> None:
    org = tmp_path / "org"
    repo = tmp_path / "repo"
    user = tmp_path / "user"
    _write_workflow(org, registry_version="1.4.0")
    _write_workflow(repo, registry_version="2.0.0")
    _write_workflow(user, registry_version="3.0.0")

    with pytest.raises(WorkflowRegistryError, match="locked by org"):
        build_workflow_registry(
            [
                _source(user, RegistryLayer.USER, "user"),
                _source(repo, RegistryLayer.REPO, "repo"),
                _source(org, RegistryLayer.ORG, "corp", locked=True),
            ]
        )


def test_non_authoritative_layers_cannot_be_marked_locked(tmp_path: Path) -> None:
    with pytest.raises(WorkflowRegistryError, match="only builtin/org"):
        _source(tmp_path, RegistryLayer.REPO, "repo", locked=True)


def test_semver_build_variants_are_ambiguous_for_unqualified_latest(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    repo = tmp_path / "repo"
    _write_workflow(builtin, registry_version="1.0.0+one", action="build")
    _write_workflow(repo, registry_version="1.0.0+two", action="test")
    registry = build_workflow_registry(
        [_source(builtin, RegistryLayer.BUILTIN, "builtin"), _source(repo, RegistryLayer.REPO, "repo")]
    )

    with pytest.raises(WorkflowRegistryError, match="ambiguous latest SemVer build variants"):
        registry.resolve("delivery")
    assert registry.resolve("delivery@1.0.0+one").identity == "delivery@1.0.0+one"


def test_list_search_and_info_are_deterministic(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_workflow(repo, "alpha", registry_version="1.0.0")
    _write_workflow(repo, "zeta", registry_version="2.0.0")
    registry = build_workflow_registry([_source(repo, RegistryLayer.REPO, "repo")])

    assert [item.name for item in registry.list()] == ["alpha", "zeta"]
    assert [item.name for item in registry.search("zet")] == ["zeta"]
    assert [item.identity for item in registry.info("alpha")] == ["alpha@1.0.0"]


def test_unicode_source_labels_and_roots_do_not_enter_canonical_path(tmp_path: Path) -> None:
    root = tmp_path / "équipe-Δ"
    _write_workflow(root, registry_version="1.0.0")
    registry = build_workflow_registry([_source(root, RegistryLayer.REPO, "équipe Δ")])
    resolved = registry.resolve("delivery")

    assert resolved.selected_provenance.source == "équipe Δ"
    assert resolved.selected_provenance.path == ".sdai/workflows/delivery.yaml"
    assert str(root) not in resolved.to_json()
    assert resolved.to_json().encode("utf-8").decode("utf-8") == resolved.to_json()


def test_filename_name_mismatch_and_bad_registry_version_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = _write_workflow(root, registry_version="1.0.0")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["name"] = "other"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(WorkflowRegistryError, match="filename/name mismatch"):
        build_workflow_registry([_source(root, RegistryLayer.REPO, "repo")])

    data["name"] = "delivery"
    data["registry_version"] = "not-semver"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(WorkflowRegistryError, match="invalid workflow registry_version"):
        build_workflow_registry([_source(root, RegistryLayer.REPO, "repo")])


def test_symlink_workflow_sources_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    real = tmp_path / "real"
    _write_workflow(real, registry_version="1.0.0")
    target_dir = root / ".sdai"
    target_dir.mkdir(parents=True)
    try:
        (target_dir / "workflows").symlink_to(real / ".sdai" / "workflows", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(WorkflowRegistryError, match="not a symlink"):
        build_workflow_registry([_source(root, RegistryLayer.REPO, "repo")])


def test_source_hash_is_separate_from_resolved_graph_hash(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_workflow(repo, registry_version="1.0.0")
    resolved = build_workflow_registry([_source(repo, RegistryLayer.REPO, "repo")]).resolve("delivery")

    assert resolved.source_sha256 != resolved.graph_sha256
    payload = resolved.as_dict()
    assert payload["sourceSha256"] == resolved.source_sha256
    assert payload["graphSha256"] == resolved.graph_sha256
    assert payload["graphResolutionSha256"] == resolved.resolution_sha256
