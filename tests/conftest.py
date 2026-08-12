from __future__ import annotations

from pathlib import Path

import pytest
import yaml


_PROMOTION_FIXTURE_MODULES = {"test_spec_promotion_v07.py"}


@pytest.fixture(autouse=True)
def _initialized_spec_promotion_governance(request: pytest.FixtureRequest) -> None:
    """Model an initialized SDAI project only for direct promotion API tests.

    The production CLI already requires `.sdai/config.yaml` and normal `sdai init`
    scaffolds `approval-policies.yaml`. These direct unit tests intentionally bypass
    the CLI, so they need the same governance precondition without weakening the
    production behavior when governance configuration is missing.
    """

    filename = Path(str(request.node.fspath)).name
    if filename not in _PROMOTION_FIXTURE_MODULES:
        return
    root = request.getfixturevalue("tmp_path")
    sdai = root / ".sdai"
    sdai.mkdir(parents=True, exist_ok=True)
    (sdai / "approval-policies.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "gates": {
                    "spec-promotion": {
                        "min_approvals": 1,
                        "required_roles": [],
                        "allowed_approvers": [],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
