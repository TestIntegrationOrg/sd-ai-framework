from __future__ import annotations

from pathlib import Path

import pytest

from sdai.architecture_policy import (
    ARCHITECTURE_POLICY_API_VERSION,
    ORG_ARCHITECTURE_POLICY_ENV,
    ArchitecturePolicyError,
    load_effective_architecture_policy,
)


def _policy() -> str:
    return f"""apiVersion: {ARCHITECTURE_POLICY_API_VERSION}
kind: ArchitectureDriftPolicy
required: true
defaultThreshold: warning
kinds: {{}}
"""


def _symlink_or_skip(link: Path, target: Path | str) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links are unavailable on this runner: {exc}")


def test_broken_repository_policy_symlink_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / ".sdai").mkdir(parents=True)
    link = root / ".sdai" / "architecture-drift-policy.yaml"
    _symlink_or_skip(link, root / "missing-policy.yaml")

    with pytest.raises(ArchitecturePolicyError, match="SDAI-ARCH-POLICY-004.*symlink"):
        load_effective_architecture_policy(root, environ={})


def test_organization_policy_leaf_symlink_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    target = tmp_path / "organization-policy.yaml"
    target.write_text(_policy(), encoding="utf-8", newline="\n")
    link = tmp_path / "organization-policy-link.yaml"
    _symlink_or_skip(link, target)

    with pytest.raises(ArchitecturePolicyError, match="SDAI-ARCH-POLICY-004.*non-symlink"):
        load_effective_architecture_policy(
            root,
            environ={ORG_ARCHITECTURE_POLICY_ENV: str(link.absolute())},
        )


def test_organization_policy_must_exist_as_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    missing = tmp_path / "missing-organization-policy.yaml"

    with pytest.raises(ArchitecturePolicyError, match="SDAI-ARCH-POLICY-004.*existing regular file"):
        load_effective_architecture_policy(
            root,
            environ={ORG_ARCHITECTURE_POLICY_ENV: str(missing.absolute())},
        )
