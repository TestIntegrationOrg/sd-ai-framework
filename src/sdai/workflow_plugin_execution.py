from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from sdai.plugin_steps import (
    EXECUTORS,
    PluginExecutorRegistry,
    PluginStepError,
    execute_prepared_plugin_step,
    prepare_plugin_step,
)
from sdai.workflow_execution import (
    WorkflowExecutionStatus,
    WorkflowLeafExecutor,
    WorkflowLeafInvocation,
    WorkflowLeafOutcome,
    default_workflow_leaf_executor,
)
from sdai.workflow_graph import WorkflowGraphNode, WorkflowGraphResolution, WorkflowNodeKind


@dataclass(frozen=True)
class WorkflowPluginLeafExecutor:
    """Trusted PluginStep adapter for bounded Workflow Engine 2 execution."""

    project_root: Path
    resolution: WorkflowGraphResolution
    registry: PluginExecutorRegistry = EXECUTORS
    environ: Mapping[str, str] | None = None
    fallback: WorkflowLeafExecutor = default_workflow_leaf_executor

    def planning_binding(self, node: WorkflowGraphNode) -> str | None:
        if node.kind != WorkflowNodeKind.PLUGIN.value:
            return None
        plugin_id = node.config.get("plugin")
        inputs = self.resolution.plugin_inputs.get(node.path)
        if not isinstance(plugin_id, str) or inputs is None:
            raise PluginStepError(
                f"SDAI-PLUGIN-011: plugin node '{node.path}' has invalid private planning data"
            )
        return prepare_plugin_step(
            self.project_root,
            plugin_id,
            node.id,
            inputs,
            environ=self.environ,
        ).sha256

    def __call__(self, invocation: WorkflowLeafInvocation) -> WorkflowLeafOutcome:
        node = invocation.node
        if node.kind != WorkflowNodeKind.PLUGIN.value:
            return self.fallback(invocation)
        plugin_id = node.config.get("plugin")
        if not isinstance(plugin_id, str):
            return WorkflowLeafOutcome(
                WorkflowExecutionStatus.FAILED,
                error=f"plugin node '{node.path}' has invalid plugin identity",
            )
        inputs = self.resolution.plugin_inputs.get(node.path)
        if inputs is None:
            return WorkflowLeafOutcome(
                WorkflowExecutionStatus.FAILED,
                error=f"plugin node '{node.path}' has no bound private inputs",
            )
        try:
            plan = prepare_plugin_step(
                self.project_root,
                plugin_id,
                node.id,
                inputs,
                environ=self.environ,
            )
            if node.config.get("inputsSha256") != plan.input_sha256:
                return WorkflowLeafOutcome(
                    WorkflowExecutionStatus.FAILED,
                    error=f"plugin node '{node.path}' input binding changed",
                )
            if invocation.planning_binding != plan.sha256:
                return WorkflowLeafOutcome(
                    WorkflowExecutionStatus.FAILED,
                    error=f"plugin node '{node.path}' execution plan changed after dispatch",
                )
            result = execute_prepared_plugin_step(
                self.project_root,
                plan,
                registry=self.registry,
                environ=self.environ,
            )
            status = (
                WorkflowExecutionStatus.SUCCEEDED
                if result.status == "passed"
                else WorkflowExecutionStatus.FAILED
            )
            return WorkflowLeafOutcome(
                status,
                result.as_dict(),
                (
                    f"plugin-manifest:{plan.plugin.manifest_sha256}",
                    f"plugin-plan:{plan.sha256}",
                ),
                None if status == WorkflowExecutionStatus.SUCCEEDED else result.summary,
            )
        except PluginStepError as exc:
            return WorkflowLeafOutcome(
                WorkflowExecutionStatus.FAILED,
                error=str(exc),
            )
