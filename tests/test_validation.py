from pathlib import Path

from sdai.models import FeatureContext, LifecycleMode
from sdai.validation import has_blockers, validate


def test_missing_artifacts_are_blocking(tmp_path: Path):
    context = FeatureContext(tmp_path, "MISSING-1")
    findings = validate(context, LifecycleMode.STANDARD)
    assert has_blockers(findings)
    assert any(f.code == "ARTIFACT_MISSING" for f in findings)
