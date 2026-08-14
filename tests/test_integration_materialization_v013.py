from __future__ import annotations

from pathlib import Path

import pytest

import sdai.integration_materialization as materialization
from sdai.extensions.registry import RegistryLayer
from sdai.integration_manifest import INTEGRATION_MANIFEST_API_VERSION, IntegrationManifest
from sdai.integration_materialization import (
    INTEGRATION_INSTALL_STATE_API_VERSION,
    INTEGRATION_STATUS_API_VERSION,
    IntegrationFileStatus,
    IntegrationMaterializationError,
    integration_status,
    load_install_state,
    materialize_integration,
    operation_journal_path,
    remove_integration,
    repair_integration,
)
from sdai.integration_registry import IntegrationRegistry, ResolvedIntegration


def _resolved(
    *,
    version: str = "1.0.0",
    projections: list[dict[str, str]] | None = None,
) -> ResolvedIntegration:
    projection_values = projections or [
        {"kind": "skill", "source": ".agents/skills", "target": ".tool/skills"}
    ]
    capabilities = sorted(
        {
            {"skill": "skills", "command": "commands", "agent-file": "agent-files"}[item["kind"]]
            for item in projection_values
        }
    )
    manifest = IntegrationManifest.from_dict(
        {
            "apiVersion": INTEGRATION_MANIFEST_API_VERSION,
            "id": "test-tool",
            "version": version,
            "displayName": "Test Tool café Δ",
            "description": "Native materialization test",
            "capabilities": capabilities,
            "projections": projection_values,
            "execution": None,
            "security": {
                "requiresNetwork": False,
                "requiresWorkspaceWrite": False,
                "environment": [],
            },
        }
    )
    registry = IntegrationRegistry()
    registry.register(
        manifest,
        layer=RegistryLayer.ORG,
        source="enterprise-catalog",
        path=f"test-tool/{version}.integration.yaml",
    )
    resolved = registry.resolve("test-tool", version)
    assert resolved is not None
    return resolved


def _write_skill_tree(root: Path, *, first: str = "Review café Δ\n", second: str = "Second skill\n") -> None:
    source = root / ".agents" / "skills"
    (source / "nested").mkdir(parents=True, exist_ok=True)
    (source / "review.md").write_text(first, encoding="utf-8", newline="\n")
    (source / "nested" / "second.md").write_text(second, encoding="utf-8", newline="\n")


def test_directory_projection_install_is_deterministic_idempotent_and_provenance_bound(tmp_path: Path) -> None:
    _write_skill_tree(tmp_path)
    resolved = _resolved()

    before = integration_status(tmp_path, resolved)
    first = materialize_integration(tmp_path, resolved)
    state_bytes = (tmp_path / ".sdai" / "integrations" / "install-state.json").read_bytes()
    second = materialize_integration(tmp_path, resolved)
    after = integration_status(tmp_path, resolved)

    assert before.status == IntegrationFileStatus.MISSING
    assert first == second
    assert after.status == IntegrationFileStatus.EXACT
    assert after.as_dict()["apiVersion"] == INTEGRATION_STATUS_API_VERSION
    assert (tmp_path / ".tool" / "skills" / "review.md").read_text(encoding="utf-8") == "Review café Δ\n"
    assert (tmp_path / ".tool" / "skills" / "nested" / "second.md").read_text(encoding="utf-8") == "Second skill\n"
    assert (tmp_path / ".sdai" / "integrations" / "install-state.json").read_bytes() == state_bytes
    state = load_install_state(tmp_path)
    assert state.as_dict()["apiVersion"] == INTEGRATION_INSTALL_STATE_API_VERSION
    assert state.integrations == (first,)
    assert first.identity == "test-tool@1.0.0"
    assert first.manifest_sha256 == resolved.manifest_sha256
    assert first.provenance_layer == "org"
    assert first.provenance_source == "enterprise-catalog"
    assert first.provenance_path == "test-tool/1.0.0.integration.yaml"
    assert [item.path for item in first.files] == [
        ".tool/skills/nested/second.md",
        ".tool/skills/review.md",
    ]
    assert not operation_journal_path(tmp_path).exists()


def test_single_file_projection_uses_target_as_exact_destination(tmp_path: Path) -> None:
    source = tmp_path / ".sdai-source"
    source.mkdir()
    (source / "AGENT.md").write_text("Agent café Δ\n", encoding="utf-8", newline="\n")
    resolved = _resolved(
        projections=[
            {"kind": "agent-file", "source": ".sdai-source/AGENT.md", "target": ".tool/AGENT.md"}
        ]
    )

    record = materialize_integration(tmp_path, resolved)

    assert len(record.files) == 1
    assert record.files[0].source_path == ".sdai-source/AGENT.md"
    assert record.files[0].path == ".tool/AGENT.md"
    assert (tmp_path / ".tool" / "AGENT.md").read_text(encoding="utf-8") == "Agent café Δ\n"


def test_unmanaged_destination_is_never_adopted_even_when_bytes_match_across_retries(tmp_path: Path) -> None:
    _write_skill_tree(tmp_path)
    resolved = _resolved()
    destination = tmp_path / ".tool" / "skills" / "review.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("Review café Δ\n", encoding="utf-8", newline="\n")

    for _ in range(2):
        report = integration_status(tmp_path, resolved)
        assert report.status == IntegrationFileStatus.UNMANAGED_CONFLICT
        with pytest.raises(IntegrationMaterializationError, match="unmanaged-conflict"):
            materialize_integration(tmp_path, resolved)
        assert destination.read_text(encoding="utf-8") == "Review café Δ\n"
        assert not operation_journal_path(tmp_path).exists()
        assert load_install_state(tmp_path).integrations == ()


def test_modified_managed_destination_is_preserved_by_repair_upgrade_and_remove(tmp_path: Path) -> None:
    _write_skill_tree(tmp_path)
    resolved = _resolved()
    materialize_integration(tmp_path, resolved)
    destination = tmp_path / ".tool" / "skills" / "review.md"
    destination.write_text("USER EDIT Δ\n", encoding="utf-8", newline="\n")

    report = integration_status(tmp_path, resolved)
    assert report.status == IntegrationFileStatus.MODIFIED
    assert any(item.path == ".tool/skills/review.md" and item.status == IntegrationFileStatus.MODIFIED for item in report.findings)
    with pytest.raises(IntegrationMaterializationError, match="user-modified"):
        repair_integration(tmp_path, resolved)
    with pytest.raises(IntegrationMaterializationError, match="user-modified"):
        materialize_integration(tmp_path, resolved)
    assert destination.read_text(encoding="utf-8") == "USER EDIT Δ\n"

    preserved = remove_integration(tmp_path, "test-tool")
    assert preserved == (".tool/skills/review.md",)
    assert destination.read_text(encoding="utf-8") == "USER EDIT Δ\n"
    assert not (tmp_path / ".tool" / "skills" / "nested" / "second.md").exists()
    assert load_install_state(tmp_path).integrations == ()
    assert remove_integration(tmp_path, "test-tool") == ()


def test_missing_and_clean_stale_files_are_repaired_without_touching_user_content(tmp_path: Path) -> None:
    _write_skill_tree(tmp_path)
    resolved = _resolved()
    materialize_integration(tmp_path, resolved)
    review = tmp_path / ".tool" / "skills" / "review.md"
    second = tmp_path / ".tool" / "skills" / "nested" / "second.md"
    review.unlink()
    (tmp_path / ".agents" / "skills" / "nested" / "second.md").write_text(
        "Updated source Δ\n", encoding="utf-8", newline="\n"
    )

    report = integration_status(tmp_path, resolved)
    assert {item.status for item in report.findings} == {
        IntegrationFileStatus.MISSING,
        IntegrationFileStatus.STALE,
    }

    repaired = repair_integration(tmp_path, resolved)

    assert review.read_text(encoding="utf-8") == "Review café Δ\n"
    assert second.read_text(encoding="utf-8") == "Updated source Δ\n"
    assert integration_status(tmp_path, resolved).status == IntegrationFileStatus.EXACT
    assert repaired == load_install_state(tmp_path).integrations[0]


def test_version_upgrade_replaces_clean_bytes_deletes_clean_obsolete_and_preserves_modified_obsolete(tmp_path: Path) -> None:
    _write_skill_tree(tmp_path)
    commands = tmp_path / ".sdai-source" / "commands"
    commands.mkdir(parents=True)
    (commands / "run.md").write_text("run v1\n", encoding="utf-8", newline="\n")
    v1 = _resolved(
        version="1.0.0",
        projections=[
            {"kind": "skill", "source": ".agents/skills", "target": ".tool/skills"},
            {"kind": "command", "source": ".sdai-source/commands", "target": ".tool/commands"},
        ],
    )
    materialize_integration(tmp_path, v1)
    obsolete_modified = tmp_path / ".tool" / "commands" / "run.md"
    obsolete_modified.write_text("user command edit\n", encoding="utf-8", newline="\n")
    (tmp_path / ".agents" / "skills" / "review.md").write_text("Review v2 café Δ\n", encoding="utf-8", newline="\n")
    v2 = _resolved(version="2.0.0")

    upgraded = materialize_integration(tmp_path, v2)

    assert upgraded.identity == "test-tool@2.0.0"
    assert upgraded.preserved_paths == (".tool/commands/run.md",)
    assert obsolete_modified.read_text(encoding="utf-8") == "user command edit\n"
    assert (tmp_path / ".tool" / "skills" / "review.md").read_text(encoding="utf-8") == "Review v2 café Δ\n"
    assert integration_status(tmp_path, v2).status == IntegrationFileStatus.EXACT


def test_clean_obsolete_file_is_removed_on_upgrade(tmp_path: Path) -> None:
    _write_skill_tree(tmp_path)
    commands = tmp_path / ".sdai-source" / "commands"
    commands.mkdir(parents=True)
    (commands / "run.md").write_text("run v1\n", encoding="utf-8")
    v1 = _resolved(
        projections=[
            {"kind": "skill", "source": ".agents/skills", "target": ".tool/skills"},
            {"kind": "command", "source": ".sdai-source/commands", "target": ".tool/commands"},
        ]
    )
    materialize_integration(tmp_path, v1)
    v2 = _resolved(version="2.0.0")

    materialize_integration(tmp_path, v2)

    assert not (tmp_path / ".tool" / "commands" / "run.md").exists()


def test_first_install_recovers_only_matching_journal_owned_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_skill_tree(tmp_path)
    resolved = _resolved()
    original_write_state = materialization._write_state

    def crash_after_bytes(*args, **kwargs):
        raise RuntimeError("simulated crash after managed writes")

    monkeypatch.setattr(materialization, "_write_state", crash_after_bytes)
    with pytest.raises(RuntimeError, match="simulated crash"):
        materialize_integration(tmp_path, resolved)
    assert operation_journal_path(tmp_path).exists()
    assert (tmp_path / ".tool" / "skills" / "review.md").exists()
    assert load_install_state(tmp_path).integrations == ()

    monkeypatch.setattr(materialization, "_write_state", original_write_state)
    recovered = materialize_integration(tmp_path, resolved)

    assert recovered.identity == resolved.identity
    assert integration_status(tmp_path, resolved).status == IntegrationFileStatus.EXACT
    assert not operation_journal_path(tmp_path).exists()


def test_interrupted_output_must_still_match_journal_before_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_skill_tree(tmp_path)
    resolved = _resolved()
    original_write_state = materialization._write_state
    monkeypatch.setattr(materialization, "_write_state", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        materialize_integration(tmp_path, resolved)
    output = tmp_path / ".tool" / "skills" / "review.md"
    output.write_text("tampered interrupted output\n", encoding="utf-8")
    monkeypatch.setattr(materialization, "_write_state", original_write_state)

    with pytest.raises(IntegrationMaterializationError, match="no longer matches planned bytes"):
        materialize_integration(tmp_path, resolved)
    assert output.read_text(encoding="utf-8") == "tampered interrupted output\n"
    assert operation_journal_path(tmp_path).exists()


def test_crash_after_state_commit_before_journal_cleanup_is_idempotently_recovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_skill_tree(tmp_path)
    resolved = _resolved()
    original_clear = materialization._clear_journal
    monkeypatch.setattr(materialization, "_clear_journal", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cleanup crash")))
    with pytest.raises(RuntimeError, match="cleanup crash"):
        materialize_integration(tmp_path, resolved)
    committed = load_install_state(tmp_path).integrations[0]
    assert operation_journal_path(tmp_path).exists()

    monkeypatch.setattr(materialization, "_clear_journal", original_clear)
    recovered = materialize_integration(tmp_path, resolved)

    assert recovered == committed
    assert not operation_journal_path(tmp_path).exists()
    assert integration_status(tmp_path, resolved).status == IntegrationFileStatus.EXACT


def test_source_target_overlap_internal_target_and_source_symlink_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "native"
    source.mkdir()
    (source / "skill.md").write_text("skill\n", encoding="utf-8")
    overlap = _resolved(
        projections=[{"kind": "skill", "source": "native", "target": "native/generated"}]
    )
    assert integration_status(tmp_path, overlap).status == IntegrationFileStatus.BROKEN
    with pytest.raises(IntegrationMaterializationError, match="overlap"):
        materialize_integration(tmp_path, overlap)

    internal = _resolved(
        projections=[{"kind": "skill", "source": "native", "target": ".sdai/integrations/native"}]
    )
    with pytest.raises(IntegrationMaterializationError, match="overlaps SDAI Integration state"):
        materialize_integration(tmp_path, internal)

    link = tmp_path / "linked-source"
    try:
        link.symlink_to(source.name, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")
    symlinked = _resolved(
        projections=[{"kind": "skill", "source": "linked-source", "target": ".tool/linked"}]
    )
    assert integration_status(tmp_path, symlinked).status == IntegrationFileStatus.BROKEN


def test_destination_symlink_ancestry_is_broken_and_never_followed(tmp_path: Path) -> None:
    _write_skill_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    tool = tmp_path / ".tool"
    try:
        tool.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")
    resolved = _resolved()

    report = integration_status(tmp_path, resolved)

    assert report.status == IntegrationFileStatus.BROKEN
    with pytest.raises(IntegrationMaterializationError):
        materialize_integration(tmp_path, resolved)
    assert list(outside.iterdir()) == []
