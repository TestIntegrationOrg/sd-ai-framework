from pathlib import Path

from sdai.artifacts import write_text
from sdai.models import FeatureContext, LifecycleMode
from sdai.orchestrator import Orchestrator
from sdai.scaffold import init_project
from sdai.validation import has_blockers, validate


def test_standard_workflow_generates_traceable_artifacts(tmp_path: Path):
    init_project(tmp_path)
    write_text(
        tmp_path / "specs" / "DEMO-1" / "00-intake.md",
        """# Feature Intake — DEMO-1\n\n## Title\nDemo\n\n## Description\nBuild a governed feature.\n""",
    )

    results = Orchestrator(tmp_path).run_workflow("DEMO-1", LifecycleMode.STANDARD)
    assert results
    context = FeatureContext(tmp_path, "DEMO-1")
    findings = validate(context, LifecycleMode.STANDARD)
    assert not has_blockers(findings)
    assert context.artifact("architecture/context.mmd").exists()
    assert context.artifact("tasks.yaml").exists()
