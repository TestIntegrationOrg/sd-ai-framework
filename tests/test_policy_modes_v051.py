from pathlib import Path

import pytest
import yaml

from sdai.agent_platform.models import AgentProfile, Capability, ExecutionMode
from sdai.artifacts import write_text
from sdai.policy import OperatingMode, PolicyError, load_effective_configuration


def _config(root: Path, mode: str = "individual") -> None:
    write_text(
        root / ".sdai" / "config.yaml",
        yaml.safe_dump(
            {
                "version": 3,
                "operating_mode": mode,
                "policy": {
                    "repository": ".sdai/policy.yaml",
                    "organization_env": "SDAI_ORG_POLICY_PATH",
                    "user_env": "SDAI_USER_POLICY_PATH",
                },
            },
            sort_keys=False,
        ),
    )


def _profile(name: str, provider: str, *, model: str | None = None) -> AgentProfile:
    return AgentProfile(
        name=name,
        provider=provider,
        capabilities=(Capability.ARCHITECTURE,),
        prompt="architect.md",
        model=model,
    )


def test_individual_mode_keeps_provider_choice_open(tmp_path: Path):
    _config(tmp_path)
    effective = load_effective_configuration(tmp_path, environ={})
    assert effective.operating_mode == OperatingMode.INDIVIDUAL
    effective.assert_profile_allowed(
        _profile("my-claude", "claude"), Capability.ARCHITECTURE, ExecutionMode.ADVISORY
    )
    effective.assert_profile_allowed(
        _profile("my-local", "custom"), Capability.ARCHITECTURE, ExecutionMode.ADVISORY
    )


def test_enterprise_mode_requires_external_organization_policy(tmp_path: Path):
    _config(tmp_path, "enterprise")
    with pytest.raises(PolicyError, match="SDAI_ORG_POLICY_PATH"):
        load_effective_configuration(tmp_path, environ={})


def test_organization_policy_constrains_but_does_not_choose_for_employee(tmp_path: Path):
    _config(tmp_path, "individual")
    org_path = tmp_path.parent / f"{tmp_path.name}-org-policy.yaml"
    write_text(
        org_path,
        yaml.safe_dump(
            {
                "version": 1,
                "providers": {
                    "allowed_profiles": ["claude-enterprise", "codex-enterprise"],
                    "allowed_providers": ["claude", "codex"],
                    "allowed_models": {
                        "claude": ["approved-claude"],
                        "codex": ["approved-codex"],
                    },
                },
                "capabilities": {
                    "architecture": {
                        "allowed_profiles": ["claude-enterprise", "codex-enterprise"]
                    }
                },
                "execution": {
                    "workspace_write": True,
                    "require_prior_approval_for_workspace_write": True,
                    "allow_force_approval_bypass": False,
                    "protected_paths": [".github/workflows/**"],
                },
                "skills": {"required": {"architecture": ["company-architecture"]}},
            },
            sort_keys=False,
        ),
    )
    env = {"SDAI_ORG_POLICY_PATH": str(org_path.resolve())}
    effective = load_effective_configuration(tmp_path, environ=env)
    assert effective.operating_mode == OperatingMode.ENTERPRISE

    # The employee may choose either approved provider/profile.
    effective.assert_profile_allowed(
        _profile("claude-enterprise", "claude", model="approved-claude"),
        Capability.ARCHITECTURE,
        ExecutionMode.ADVISORY,
    )
    effective.assert_profile_allowed(
        _profile("codex-enterprise", "codex", model="approved-codex"),
        Capability.ARCHITECTURE,
        ExecutionMode.ADVISORY,
    )

    with pytest.raises(PolicyError, match="not permitted"):
        effective.assert_profile_allowed(
            _profile("personal-gemini", "gemini", model="personal"),
            Capability.ARCHITECTURE,
            ExecutionMode.ADVISORY,
        )

    assert effective.require_prior_approval_for_workspace_write
    assert not effective.allow_force_approval_bypass
    assert "company-architecture" in effective.required_skills(Capability.ARCHITECTURE)
    assert ".github/workflows/**" in effective.protected_paths


def test_repo_and_user_policy_can_only_narrow_org_provider_set(tmp_path: Path):
    _config(tmp_path)
    write_text(
        tmp_path / ".sdai" / "policy.yaml",
        """version: 1
providers:
  allowed_profiles: [claude-enterprise]
""",
    )
    org_path = tmp_path.parent / f"{tmp_path.name}-org-policy-narrow.yaml"
    write_text(
        org_path,
        """version: 1
providers:
  allowed_profiles: [claude-enterprise, codex-enterprise]
""",
    )
    effective = load_effective_configuration(
        tmp_path, environ={"SDAI_ORG_POLICY_PATH": str(org_path.resolve())}
    )
    assert effective.allowed_profiles == frozenset({"claude-enterprise"})


def test_org_policy_cannot_be_supplied_from_inside_repository(tmp_path: Path):
    _config(tmp_path, "enterprise")
    inside = tmp_path / "org.yaml"
    write_text(inside, "version: 1\n")
    with pytest.raises(PolicyError, match="outside the project"):
        load_effective_configuration(
            tmp_path, environ={"SDAI_ORG_POLICY_PATH": str(inside.resolve())}
        )


def test_repo_cannot_redirect_organization_policy_environment(tmp_path: Path):
    _config(tmp_path)
    config_path = tmp_path / ".sdai" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["policy"]["organization_env"] = "IGNORE_COMPANY_POLICY"
    write_text(config_path, yaml.safe_dump(config, sort_keys=False))
    org_path = tmp_path.parent / f"{tmp_path.name}-canonical-org.yaml"
    write_text(org_path, """version: 1
providers:
  allowed_providers: [claude]
""")
    effective = load_effective_configuration(
        tmp_path, environ={"SDAI_ORG_POLICY_PATH": str(org_path.resolve())}
    )
    assert effective.operating_mode == OperatingMode.ENTERPRISE
    with pytest.raises(PolicyError):
        effective.assert_profile_allowed(
            _profile("codex", "codex"), Capability.ARCHITECTURE, ExecutionMode.ADVISORY
        )


def test_profile_model_rule_cannot_bypass_provider_model_rule(tmp_path: Path):
    _config(tmp_path)
    write_text(
        tmp_path / ".sdai" / "policy.yaml",
        """version: 1
providers:
  allowed_models:
    claude-enterprise: [evil-model]
""",
    )
    org_path = tmp_path.parent / f"{tmp_path.name}-models-org.yaml"
    write_text(
        org_path,
        """version: 1
providers:
  allowed_models:
    claude: [approved-model]
""",
    )
    effective = load_effective_configuration(
        tmp_path, environ={"SDAI_ORG_POLICY_PATH": str(org_path.resolve())}
    )
    with pytest.raises(PolicyError, match="not permitted"):
        effective.assert_profile_allowed(
            _profile("claude-enterprise", "claude", model="evil-model"),
            Capability.ARCHITECTURE,
            ExecutionMode.ADVISORY,
        )


def test_enterprise_environment_allowlist_fails_closed_when_omitted(tmp_path: Path):
    _config(tmp_path)
    org_path = tmp_path.parent / f"{tmp_path.name}-env-org.yaml"
    write_text(org_path, "version: 1\n")
    effective = load_effective_configuration(
        tmp_path, environ={"SDAI_ORG_POLICY_PATH": str(org_path.resolve())}
    )
    assert effective.environment_allowlist == frozenset()


def test_policy_rejects_unknown_keys(tmp_path: Path):
    _config(tmp_path)
    write_text(tmp_path / ".sdai" / "policy.yaml", "version: 1\nexecuton: {}\n")
    with pytest.raises(PolicyError, match="unsupported key"):
        load_effective_configuration(tmp_path, environ={})
