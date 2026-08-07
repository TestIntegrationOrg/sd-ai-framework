from pathlib import Path

import pytest

from sdai.governance import GovernanceError, record_approval
from sdai.models import FeatureContext


def test_feature_id_cannot_escape_specs_workspace(tmp_path: Path):
    with pytest.raises(ValueError):
        FeatureContext(tmp_path, "../escape")
    with pytest.raises(ValueError):
        FeatureContext(tmp_path, "/absolute")


def test_artifact_path_cannot_escape_feature_workspace(tmp_path: Path):
    context = FeatureContext(tmp_path, "SAFE-1")
    with pytest.raises(ValueError):
        context.artifact("../../secret")
    with pytest.raises(ValueError):
        context.artifact("/tmp/secret")
    assert context.artifact("architecture/design.md") == tmp_path.resolve() / "specs" / "SAFE-1" / "architecture" / "design.md"


def test_direct_approval_api_cannot_escape_feature_workspace(tmp_path: Path):
    context = FeatureContext(tmp_path, "SAFE-1")
    with pytest.raises(GovernanceError):
        record_approval(context, "../../outside", approved_by="architect@example.com")
