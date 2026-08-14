from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.execution_ledger import create_execution_run
from sdai.extensions.registry import RegistryLayer
from sdai.pack_integrity import build_pack_content_index
from sdai.pack_lifecycle import install_from_local
from sdai.pack_lock import PackLock, PackLockEntry
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, load_pack_manifest
from sdai.plugin_steps import (
    PluginExecutorRegistry,
    PluginManifestSource,
    PluginResult,
    PluginStepError,
    build_plugin_step_registry,
    load_plugin_manifest,
    prepare_plugin_step,
)
from sdai.workflow_execution import (
    WorkflowExecutionStatus,
    WorkflowLeafOutcome,
    execute_workflow_graph,
)
from sdai.workflow_graph import load_workflow_graph
from sdai.workflow_plugin_execution import WorkflowPluginLeafExecutor


FEATURE = "WF2-PLUGIN-100"
BASELINE = "c" * 40


def _init(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 1\n", encoding="utf-8")


def _plugin(
    root: Path,
    plugin_id: str = "custom",
    *,
    version: str = "1.0.0",
    publisher: str = "acme",
    executor: str = "custom-executor",
    workspace_write: bool = False,
    environment: tuple[str, ...] = (),
) -> Path:
    path = root / ".sdai" / "plugin-steps" / f"{plugin_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "sdai/v1",
                "kind": "PluginStep",
                "metadata": {"id": plugin_id, "version": version},
                "spec": {
                    "publisher": publisher,
                    "executor": executor,
                    "permissions": {
                        "filesystem": {"read": [], "write": []},
                        "network": False,
                        "environment": list(environment),
                        "commands": [],
                        "workspace_write": workspace_write,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _policy(
    root: Path,
    plugin_id: str = "custom",
    *,
    publisher: str = "acme",
    workspace_write: bool = False,
    environment: tuple[str, ...] = (),
) -> None:
    path = root / ".sdai" / "plugin-policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "allowed_plugins": [plugin_id],
                "trusted_publishers": [publisher],
                "permissions": {
                    "filesystem": {"read": [], "write": []},
                    "network": False,
                    "environment": list(environment),
                    "commands": [],
                    "workspace_write": workspace_write,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _source(
    root: Path,
    layer: RegistryLayer,
    label: str,
    *,
    locks: tuple[str, ...] = (),
) -> PluginManifestSource:
    return PluginManifestSource(root, layer, label, locked_plugins=locks)


class _PassingExecutor:
    def __init__(self, *, crash_on: int | None = None) -> None:
        self.calls = 0
        self.crash_on = crash_on

    def execute(self, plan, services):
        self.calls += 1
        if self.calls == self.crash_on:
            raise RuntimeError("simulated plugin process loss")
        return PluginResult("passed", "ok", data={"call": self.calls})


def _executor_registry(executor: _PassingExecutor) -> PluginExecutorRegistry:
    registry = PluginExecutorRegistry()
    registry.register("custom-executor", executor)
    return registry


def test_layered_registry_selects_latest_with_deterministic_provenance_and_hashes(
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack"
    repo = tmp_path / "répo-Δ"
    _plugin(pack, version="1.0.0")
    _plugin(repo, version="2.0.0")
    sources = [
        _source(repo, RegistryLayer.REPO, "répository Δ"),
        _source(pack, RegistryLayer.PACK, "pack:acme/custom"),
    ]

    forward = build_plugin_step_registry(sources)
    reverse = build_plugin_step_registry(list(reversed(sources)))
    resolved = forward.resolve("custom")

    assert resolved.registration.identity == "custom@2.0.0"
    assert resolved.manifest.source_layer == RegistryLayer.REPO
    assert resolved.manifest.manifest_sha256.startswith("sha256:")
    assert forward.to_json() == reverse.to_json()
    assert "répository Δ" in resolved.to_json()


def test_exact_conflict_duplicate_and_org_lock_fail_closed(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _plugin(first, version="1.0.0")
    _plugin(second, version="1.0.0", executor="different-executor")
    with pytest.raises(PluginStepError, match="conflicting exact manifests"):
        build_plugin_step_registry(
            [
                _source(first, RegistryLayer.PACK, "pack"),
                _source(second, RegistryLayer.REPO, "repo"),
            ]
        )

    duplicate = tmp_path / "duplicate"
    _plugin(duplicate)
    legacy = duplicate / ".sdai" / "extensions" / "plugin-steps" / "custom.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes((duplicate / ".sdai" / "plugin-steps" / "custom.yaml").read_bytes())
    with pytest.raises(PluginStepError, match="more than one location"):
        build_plugin_step_registry([_source(duplicate, RegistryLayer.REPO, "repo")])

    org = tmp_path / "org"
    repo = tmp_path / "repo"
    _plugin(org, version="1.0.0")
    _plugin(repo, version="2.0.0")
    with pytest.raises(PluginStepError, match="locked by org"):
        build_plugin_step_registry(
            [
                _source(repo, RegistryLayer.REPO, "repo"),
                _source(org, RegistryLayer.ORG, "corp", locks=("custom",)),
            ]
        )


def test_default_discovery_honors_org_lock_and_policy_cannot_be_widened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init(tmp_path)
    org = tmp_path.parent / "org-plugins"
    _plugin(org, environment=("TOKEN",))
    _plugin(tmp_path, version="2.0.0")
    _policy(tmp_path, environment=("TOKEN",))
    monkeypatch.setenv("SDAI_ORG_PLUGIN_STEP_ROOTS", str(org.resolve()))
    monkeypatch.setenv("SDAI_ORG_PLUGIN_STEP_LOCKS", "custom")

    with pytest.raises(PluginStepError, match="locked by org"):
        load_plugin_manifest(tmp_path, "custom")

    (tmp_path / ".sdai" / "plugin-steps" / "custom.yaml").unlink()
    _policy(tmp_path, environment=("TOKEN", "EXTRA"))
    org_policy = tmp_path.parent / "org-plugin-policy.yaml"
    org_policy.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "allowed_plugins": ["custom"],
                "trusted_publishers": ["acme"],
                "permissions": {
                    "filesystem": {"read": [], "write": []},
                    "network": False,
                    "environment": [],
                    "commands": [],
                    "workspace_write": False,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SDAI_ORG_PLUGIN_POLICY_PATH", str(org_policy.resolve()))
    with pytest.raises(PluginStepError, match="environment permission denied"):
        prepare_plugin_step(tmp_path, "custom", "step", {})


def test_builtin_extension_first_sample_still_requires_registered_executor(tmp_path: Path) -> None:
    _init(tmp_path)
    manifest = load_plugin_manifest(tmp_path, "evidence-summary")
    assert manifest.source_layer == RegistryLayer.BUILTIN
    assert manifest.locked is True
    assert manifest.publisher == "sdai"
    plan = prepare_plugin_step(tmp_path, "evidence-summary", "summarize", {"target": "café"})
    assert plan.plugin.manifest_sha256 == manifest.manifest_sha256
    assert plan.sha256.startswith("sha256:")
    assert plan.as_dict()["executorId"] == "evidence-summary"
    assert "inputs" not in plan.as_dict()


def test_pack_installed_plugin_is_discovered_only_from_managed_bytes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    pack = tmp_path / "pack"
    _init(project)
    _plugin(pack, "packed", publisher="packco", executor="custom-executor")
    (pack / "pack.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": PACK_MANIFEST_API_VERSION,
                "id": "plugin-pack",
                "publisher": "packco",
                "version": "1.0.0",
                "description": "Pack plugin",
                "capabilities": ["plugin-steps"],
                "contentRoots": [".sdai/plugin-steps"],
                "dependencies": [],
                "compatibility": {
                    "framework": ">=0.14.0,<1.0.0",
                    "apis": ["sdai.plugin-step-registry/v2"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    manifest = load_pack_manifest(pack / "pack.yaml", pack_root=pack)
    content = build_pack_content_index(pack, manifest)
    lock = PackLock(
        roots=(manifest.identity,),
        packages=(
            PackLockEntry(
                publisher=manifest.publisher,
                id=manifest.id,
                version=manifest.version,
                source="file://pack/plugin-pack",
                manifest_sha256=manifest.sha256,
                content_sha256=content.sha256,
                dependencies=(),
            ),
        ),
    )
    install_from_local(project, pack, lock, manifest.coordinate)
    _policy(project, "packed", publisher="packco")

    loaded = load_plugin_manifest(project, "packed")
    assert loaded.source_layer == RegistryLayer.PACK
    assert loaded.source_label == "pack:packco/plugin-pack@1.0.0"

    managed = (
        project
        / ".sdai"
        / "installed-packs"
        / "packco"
        / "plugin-pack"
        / "1.0.0"
        / ".sdai"
        / "plugin-steps"
        / "packed.yaml"
    )
    managed.write_text(managed.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    with pytest.raises(PluginStepError, match="managed Pack plugin file changed"):
        load_plugin_manifest(project, "packed")


def test_plugin_leaf_inside_fan_out_resumes_without_repeating_completed_call(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    _plugin(tmp_path)
    _policy(tmp_path)
    workflow = tmp_path / ".sdai" / "workflows" / "plugins.yaml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        yaml.safe_dump(
            {
                "version": 9,
                "name": "plugins",
                "validation_mode": "standard",
                "steps": [
                    {
                        "id": "targets",
                        "type": "fan-out",
                        "items": {"literal": ["api", "web"]},
                        "as": "target",
                        "max_items": 2,
                        "max_concurrency": 1,
                        "steps": [
                            {
                                "id": "custom-check",
                                "type": "plugin",
                                "plugin": "custom",
                                "inputs": {"mode": "review"},
                            }
                        ],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    feature = tmp_path / "specs" / FEATURE
    feature.mkdir(parents=True)
    (feature / "00-intake.md").write_text("# Plugin execution\n", encoding="utf-8")
    ledger = create_execution_run(
        tmp_path,
        FEATURE,
        "plugins",
        BASELINE,
        run_id="plugin-run",
    )
    resolution = load_workflow_graph(tmp_path, "plugins")
    executor = _PassingExecutor(crash_on=2)
    adapter = WorkflowPluginLeafExecutor(
        tmp_path,
        resolution,
        registry=_executor_registry(executor),
    )

    with pytest.raises(RuntimeError, match="process loss"):
        execute_workflow_graph(resolution, ledger, leaf_executor=adapter)
    completed = execute_workflow_graph(resolution, ledger, leaf_executor=adapter)

    assert completed.status == WorkflowExecutionStatus.SUCCEEDED
    assert executor.calls == 3
    tasks = ledger.reconstruct().tasks
    assert len(tasks) == 2 and all(item.status == "completed" for item in tasks)
    assert all(item.bindings for item in tasks)


def test_changed_plugin_manifest_invalidates_prior_leaf_plan_before_resume(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    manifest_path = _plugin(tmp_path)
    _policy(tmp_path)
    workflow = tmp_path / ".sdai" / "workflows" / "plugins.yaml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        yaml.safe_dump(
            {
                "version": 9,
                "name": "plugins",
                "validation_mode": "standard",
                "steps": [
                    {
                        "id": "custom-check",
                        "type": "plugin",
                        "plugin": "custom",
                        "inputs": {"mode": "review"},
                    },
                    {"id": "approve", "type": "approval", "gate": "release"},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    feature = tmp_path / "specs" / FEATURE
    feature.mkdir(parents=True)
    (feature / "00-intake.md").write_text("# Plugin plan invalidation\n", encoding="utf-8")
    ledger = create_execution_run(
        tmp_path,
        FEATURE,
        "plugins",
        BASELINE,
        run_id="changed-plugin-run",
    )
    resolution = load_workflow_graph(tmp_path, "plugins")
    executor = _PassingExecutor()
    approvals = iter((False, True))

    def fallback(invocation):
        if next(approvals):
            return WorkflowLeafOutcome(WorkflowExecutionStatus.SUCCEEDED, {"approved": True})
        return WorkflowLeafOutcome(WorkflowExecutionStatus.PAUSED, error="pending")

    adapter = WorkflowPluginLeafExecutor(
        tmp_path,
        resolution,
        registry=_executor_registry(executor),
        fallback=fallback,
    )
    paused = execute_workflow_graph(resolution, ledger, leaf_executor=adapter)
    assert paused.status == WorkflowExecutionStatus.PAUSED
    assert executor.calls == 1

    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    raw["metadata"]["version"] = "1.0.1"
    manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    completed = execute_workflow_graph(resolution, ledger, leaf_executor=adapter)

    assert completed.status == WorkflowExecutionStatus.SUCCEEDED
    assert executor.calls == 2
    plugin_registrations = [
        event
        for event in ledger.load_events()
        if event.kind == "task.registered" and event.payload.get("nodeKind") == "plugin"
    ]
    assert len(plugin_registrations) == 2
    assert len({event.payload["leafPlanningBinding"] for event in plugin_registrations}) == 2
