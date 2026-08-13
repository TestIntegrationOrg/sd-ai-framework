from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from sdai.agent_platform.models import AgentInvocation
from sdai.artifact_schemas import ArtifactSchemaError, load_artifact_schema_graph
from sdai.artifact_state import (
    ArtifactEvidenceInput,
    ArtifactFreshness,
    evaluate_artifact_states,
    record_artifact_state,
)
from sdai.orchestrator import Orchestrator
from sdai.plugin_steps import PluginStepError, prepare_plugin_step
from sdai.version_entrypoint import main as sdai_main
from sdai.workflows import WorkflowConfigError, load_workflow
from sdai.worktree_isolation import create_worktree_session


FEATURE = "V08-STATE"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _write_yaml(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _artifact_path(root: Path, artifact_id: str) -> Path:
    names = {
        "requirements": "requirements.md",
        "architecture": "architecture.md",
        "plan": "plan.md",
        "tasks": "tasks.md",
        "tests": "tests.md",
        "verification": "verification.md",
    }
    return root / "specs" / "changes" / FEATURE / names[artifact_id]


def _create_artifact_chain(root: Path) -> Path:
    for artifact_id in (
        "requirements",
        "architecture",
        "plan",
        "tasks",
        "tests",
        "verification",
    ):
        _write(
            _artifact_path(root, artifact_id),
            f"# {artifact_id}\n\n0.8 release evidence café Δ for {artifact_id}.\n",
        )
    evidence = _write(
        root / "specs" / "changes" / FEATURE / "quality" / "architecture.yaml",
        "version: 1\nstatus: passed\n",
    )
    return evidence


def _record_artifact_chain(root: Path, evidence: Path) -> None:
    graph = load_artifact_schema_graph(root, environ={})
    assert tuple(graph.topological_order)[:2] == ("requirements", "architecture")
    for artifact_id in graph.topological_order:
        if artifact_id not in {
            "requirements",
            "architecture",
            "plan",
            "tasks",
            "tests",
            "verification",
        }:
            continue
        bindings = (
            (
                ArtifactEvidenceInput(
                    "validation",
                    "architecture-validation",
                    evidence.relative_to(root).as_posix(),
                ),
            )
            if artifact_id == "architecture"
            else ()
        )
        record_artifact_state(
            root,
            FEATURE,
            artifact_id,
            risk="standard",
            evidence=bindings,
            environ={},
        )


def _artifact_manifest(schema_id: str, artifacts: list[dict[str, object]]) -> dict[str, object]:
    return {
        "apiVersion": "sdai/v1",
        "kind": "ArtifactSchema",
        "metadata": {"id": schema_id, "version": "1.0.0"},
        "spec": {"artifacts": artifacts},
    }


def _component(root: Path) -> None:
    _write_yaml(
        root / ".sdai" / "workflow-components" / "review-suite.yaml",
        {
            "apiVersion": "sdai/v1",
            "kind": "WorkflowComponent",
            "metadata": {"id": "review-suite", "version": "1.0.0"},
            "spec": {
                "inputs": {"prefix": {"type": "string", "required": True}},
                "requires": [],
                "steps": [
                    {
                        "id": "${{ inputs.prefix }}-review",
                        "type": "agent",
                        "agent": "code-reviewer",
                        "capability": "review",
                        "mode": "advisory",
                    }
                ],
            },
        },
    )


def _workflow(root: Path) -> None:
    _write_yaml(
        root / ".sdai" / "workflows" / "composed.yaml",
        {
            "version": 7,
            "name": "composed",
            "validation_mode": "standard",
            "inputs": {"prefix": {"type": "string", "default": "payments"}},
            "steps": [
                {
                    "uses": "component:review-suite",
                    "with": {"prefix": "${{ inputs.prefix }}"},
                },
                {"id": "base-validation", "type": "validate"},
            ],
        },
    )


def _overlay(
    path: Path,
    overlay_id: str,
    operations: list[dict[str, object]],
) -> Path:
    return _write_yaml(
        path,
        {
            "version": 1,
            "id": overlay_id,
            "workflow": "composed",
            "operations": operations,
            "hooks": {},
        },
    )


def _plugin(root: Path) -> None:
    _write_yaml(
        root / ".sdai" / "plugin-steps" / "release-check.yaml",
        {
            "apiVersion": "sdai/v1",
            "kind": "PluginStep",
            "metadata": {"id": "release-check", "version": "1.0.0"},
            "spec": {
                "publisher": "acme",
                "executor": "release-check-executor",
                "permissions": {
                    "filesystem": {"read": [], "write": []},
                    "network": False,
                    "environment": [],
                    "commands": [],
                    "workspace_write": False,
                },
            },
        },
    )
    _write_yaml(
        root / ".sdai" / "plugin-policy.yaml",
        {
            "version": 1,
            "allowed_plugins": ["release-check"],
            "denied_plugins": [],
            "trusted_publishers": ["acme"],
            "permissions": {
                "filesystem": {"read": [], "write": []},
                "network": False,
                "environment": [],
                "commands": [],
                "workspace_write": False,
            },
        },
    )


def _git_executable() -> str:
    executable = shutil.which("git")
    if not executable:
        pytest.skip("git is required for the 0.8 worktree release gate")
    return executable


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [_git_executable(), *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "Worktree Release Ω"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "sdai-release@example.invalid")
    _git(root, "config", "user.name", "SDAI Release Gate")
    _git(root, "config", "core.autocrlf", "false")
    if (_git(root, "branch", "--show-current").stdout or "").strip() != "main":
        _git(root, "checkout", "-b", "main")
    _write(root / ".sdai" / "config.yaml", "version: 1\n")
    _write(root / ".sdai" / "policy.yaml", "version: 1\nrelease: guarded\n")
    _write(root / "README.md", "# Worktree release café Δ\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "verified 0.8 baseline")
    return root


def test_v08_artifact_graph_staleness_and_evidence_invalidation_are_transitive(
    tmp_path: Path,
) -> None:
    evidence = _create_artifact_chain(tmp_path)
    _record_artifact_chain(tmp_path, evidence)

    initial = evaluate_artifact_states(tmp_path, FEATURE, risk="standard", environ={})
    assert all(item.freshness is ArtifactFreshness.FRESH for item in initial.states)

    evidence.write_text(
        "version: 1\nstatus: failed\n",
        encoding="utf-8",
        newline="\n",
    )
    evidence_changed = evaluate_artifact_states(
        tmp_path,
        FEATURE,
        risk="standard",
        environ={},
    ).by_id()
    assert evidence_changed["requirements"].freshness is ArtifactFreshness.FRESH
    for artifact_id in ("architecture", "plan", "tasks", "tests", "verification"):
        assert evidence_changed[artifact_id].freshness is ArtifactFreshness.STALE

    requirements = _artifact_path(tmp_path, "requirements")
    requirements.write_text(
        requirements.read_text(encoding="utf-8") + "\n- FR-NEW: upstream changed Ω\n",
        encoding="utf-8",
        newline="\n",
    )
    requirement_changed = evaluate_artifact_states(
        tmp_path,
        FEATURE,
        risk="standard",
        environ={},
    ).by_id()
    for artifact_id in (
        "requirements",
        "architecture",
        "plan",
        "tasks",
        "tests",
        "verification",
    ):
        assert requirement_changed[artifact_id].freshness is ArtifactFreshness.STALE
    assert "\\" not in evaluate_artifact_states(
        tmp_path,
        FEATURE,
        risk="standard",
        environ={},
    ).to_json()


def test_v08_org_artifact_and_workflow_controls_cannot_be_weakened(
    tmp_path: Path,
) -> None:
    org_schema = _write_yaml(
        tmp_path / "org-artifacts.yaml",
        _artifact_manifest(
            "org-security",
            [
                {
                    "id": "security-evidence",
                    "path": "specs/changes/{feature}/security-evidence.md",
                    "type": "markdown",
                    "required": True,
                    "depends_on": ["requirements"],
                }
            ],
        ),
    )
    _write_yaml(
        tmp_path / ".sdai" / "schemas" / "weaken.yaml",
        _artifact_manifest(
            "repo-weaken",
            [{"id": "security-evidence", "required": False}],
        ),
    )
    with pytest.raises(
        ArtifactSchemaError,
        match="SDAI-SCHEMA-004.*required by organization",
    ):
        load_artifact_schema_graph(
            tmp_path,
            environ={"SDAI_ORG_SCHEMA_PATH": str(org_schema.resolve())},
        )

    workflow_root = tmp_path / "workflow"
    _component(workflow_root)
    _workflow(workflow_root)
    org_overlay = _overlay(
        workflow_root / "org-overlay.yaml",
        "org-control",
        [
            {
                "op": "append",
                "step": {"id": "org-security-gate", "type": "validate"},
            }
        ],
    )
    _overlay(
        workflow_root / ".sdai" / "workflow-overlays" / "repo.yaml",
        "repo-extension",
        [
            {
                "op": "add-before",
                "target": "org-security-gate",
                "step": {"id": "repo-precheck", "type": "validate"},
            }
        ],
    )
    definition = load_workflow(
        workflow_root,
        "composed",
        environ={"SDAI_ORG_WORKFLOW_OVERLAY_PATH": str(org_overlay.resolve())},
    )
    assert [step.id for step in definition.steps] == [
        "payments-review",
        "base-validation",
        "repo-precheck",
        "org-security-gate",
    ]
    assert definition.components[0].inputs["prefix"] == "payments"
    assert "org-security-gate" in definition.mandatory_steps

    _overlay(
        workflow_root / ".sdai" / "workflow-overlays" / "repo.yaml",
        "repo-weaken",
        [{"op": "disable", "target": "org-security-gate"}],
    )
    with pytest.raises(
        WorkflowConfigError,
        match="SDAI-WFOVER-004.*organization-mandated step 'org-security-gate'",
    ):
        load_workflow(
            workflow_root,
            "composed",
            environ={"SDAI_ORG_WORKFLOW_OVERLAY_PATH": str(org_overlay.resolve())},
        )


def test_v08_plugin_repo_allow_cannot_bypass_org_deny(tmp_path: Path) -> None:
    _plugin(tmp_path)
    allowed = prepare_plugin_step(
        tmp_path,
        "release-check",
        "scan",
        inputs={"path": "café/Δ"},
    )
    assert allowed.plugin.id == "release-check"
    assert allowed.permissions.workspace_write is False

    org_policy = _write_yaml(
        tmp_path / "org-plugin-policy.yaml",
        {
            "version": 1,
            "allowed_plugins": ["release-check"],
            "denied_plugins": ["release-check"],
            "trusted_publishers": ["acme"],
            "permissions": {
                "filesystem": {"read": [], "write": []},
                "network": False,
                "environment": [],
                "commands": [],
                "workspace_write": False,
            },
        },
    )
    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-003.*denied"):
        prepare_plugin_step(
            tmp_path,
            "release-check",
            "scan",
            inputs={"path": "café/Δ"},
            environ={"SDAI_ORG_PLUGIN_POLICY_PATH": str(org_policy.resolve())},
        )


def test_v08_worktree_execution_starts_from_verified_clean_baseline_and_records_evidence(
    tmp_path: Path,
) -> None:
    root = _git_repo(tmp_path)
    source_commit = (_git(root, "rev-parse", "HEAD").stdout or "").strip()
    source_tree = (_git(root, "rev-parse", "HEAD^{tree}").stdout or "").strip()

    session = create_worktree_session(root, "V08-Worktree-Δ")

    assert session.baseline.commit == source_commit
    assert session.baseline.tree == source_tree
    assert session.baseline.clean is True
    assert session.worktree_path.is_dir()
    assert not session.worktree_path.is_relative_to(root)
    assert (_git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout or "") == ""

    generated = session.worktree_path / "implementation café.txt"
    generated.write_text("isolated Δ change\n", encoding="utf-8")
    cleanup = session.finalize("success", cleanup_requested=True)
    assert cleanup == "preserved-dirty-cleanup-refused"
    assert generated.is_file()
    evidence = json.loads(session.evidence_path.read_text(encoding="utf-8"))
    assert evidence["source"]["commit"] == source_commit
    assert evidence["source"]["tree"] == source_tree
    assert evidence["worktree"]["dirty"] is True
    assert evidence["worktree"]["cleanup"] == "preserved-dirty-cleanup-refused"
    assert (_git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout or "") == ""

    _git(root, "worktree", "remove", "--force", str(session.worktree_path))
    _git(root, "branch", "-D", session.worktree_branch)


def test_v08_preserves_v06_v07_manual_semantic_agent_and_provider_override(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Backward Compatibility Ω"
    root.mkdir()
    assert sdai_main(["init", "--path", str(root)]) == 0
    capsys.readouterr()

    execution = Orchestrator(root).run_manual_step(
        "V08-PROVIDER",
        "enterprise",
        "architecture-review",
        dry_run=True,
        agent_override="architect",
        profile_override="codex",
    )

    assert execution.status == "dry-run"
    assert isinstance(execution.result, AgentInvocation)
    assert execution.result.agent_name == "architect"
    assert execution.result.profile.name == "codex"
    assert execution.result.profile.provider == "codex"
