from __future__ import annotations

from pathlib import Path

import yaml

from sdai.artifacts import write_text
from sdai.governance import scaffold_approval_policies, scaffold_governance
from sdai.policy import scaffold_repository_policy
from sdai.quality_gates import scaffold_quality_gates


def _integration_config() -> str:
    data = {
        "version": 1,
        "github": {
            "enabled": True,
            "transport": "gh-cli",
            "credentials": "managed-by-gh",
        },
        "jira": {
            "enabled": False,
            "transport": "rest",
            "environment": [
                "JIRA_BASE_URL",
                "JIRA_EMAIL",
                "JIRA_API_TOKEN",
                "JIRA_BEARER_TOKEN",
            ],
        },
    }
    return yaml.safe_dump(data, sort_keys=False)


def _approval_policy() -> str:
    # Preserve the v0.3 architecture gate (identity-only) while demonstrating
    # role-enforced enterprise gates for new workflows.
    data = yaml.safe_load(scaffold_approval_policies())
    data["gates"]["architecture"] = {
        "min_approvals": 1,
        "required_roles": [],
        "allowed_approvers": [],
    }
    data["gates"]["enterprise-architecture"] = {
        "min_approvals": 1,
        "required_roles": ["architect"],
        "allowed_approvers": [],
    }
    data["gates"]["enterprise-security"] = {
        "min_approvals": 1,
        "required_roles": ["security"],
        "allowed_approvers": [],
    }
    return yaml.safe_dump(data, sort_keys=False)


def install_v04_scaffold(root: Path) -> list[Path]:
    """Add enterprise-capable configuration without overwriting team customizations.

    The same files are useful in individual mode; organization policy is optional until
    config.yaml selects enterprise mode or SDAI_ORG_POLICY_PATH is supplied.
    """
    if not (root / ".sdai" / "config.yaml").exists():
        raise FileNotFoundError("Not an SD-AI project. Run `sdai init` first.")

    defaults = {
        root / ".sdai" / "governance.yaml": scaffold_governance(),
        root / ".sdai" / "approval-policies.yaml": _approval_policy(),
        root / ".sdai" / "quality-gates.yaml": scaffold_quality_gates(),
        root / ".sdai" / "integrations.yaml": _integration_config(),
        root / ".sdai" / "policy.yaml": scaffold_repository_policy(),
    }
    created: list[Path] = []
    for path, content in defaults.items():
        if not path.exists():
            created.append(write_text(path, content, overwrite=False))
    return created
