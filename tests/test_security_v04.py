from __future__ import annotations

from pathlib import Path
import sys

import pytest
import yaml

from sdai.artifacts import write_text
from sdai.integrations.jira import JiraClient, JiraIntegrationError
from sdai.models import FeatureContext
from sdai.quality_gates import QualityGateRunner


def test_jira_requires_tls():
    with pytest.raises(JiraIntegrationError):
        JiraClient("http://jira.example.invalid", email="u", api_token="t")


def test_quality_gate_redacts_secret_environment_values(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEMO_API_TOKEN", "super-secret-value")
    config = {
        "version": 1,
        "gates": {
            "redaction": {
                "enabled": True,
                "command": [
                    sys.executable,
                    "-c",
                    "import os; print(os.environ['DEMO_API_TOKEN'])",
                ],
                "timeout_seconds": 10,
                "success_exit_codes": [0],
                "max_output_chars": 10000,
            }
        },
    }
    write_text(tmp_path / ".sdai" / "quality-gates.yaml", yaml.safe_dump(config, sort_keys=False))
    context = FeatureContext(tmp_path, "SEC-1")
    result = QualityGateRunner(tmp_path).run("redaction", context=context)
    assert "super-secret-value" not in result.output
    assert "[REDACTED]" in result.output
    artifact = context.artifact("quality-gates/redaction.md").read_text(encoding="utf-8")
    assert "super-secret-value" not in artifact
