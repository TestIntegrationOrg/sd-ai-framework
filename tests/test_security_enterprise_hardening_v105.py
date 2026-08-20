from pathlib import Path

import pytest
import yaml

from sdai.agent_platform.models import Capability
from sdai.artifacts import write_text
from sdai.policy import CORE_PROTECTED_PATHS, load_effective_configuration
from sdai.providers.cli import build_provider_environment


def _config(root: Path) -> None:
    write_text(
        root / ".sdai" / "config.yaml",
        yaml.safe_dump(
            {
                "version": 3,
                "operating_mode": "individual",
                "policy": {"repository": ".sdai/policy.yaml"},
            },
            sort_keys=False,
        ),
    )


def _external_policy(root: Path, name: str, content: str) -> Path:
    path = root.parent / f"{root.name}-{name}.yaml"
    write_text(path, content)
    return path.resolve()


def test_enterprise_org_omitted_environment_allowlist_cannot_be_widened_by_repo(
    tmp_path: Path,
):
    _config(tmp_path)
    write_text(
        tmp_path / ".sdai" / "policy.yaml",
        """version: 1
execution:
  environment_allowlist: [HOME, OPENAI_API_KEY, HTTPS_PROXY]
""",
    )
    org_path = _external_policy(tmp_path, "org-no-env", "version: 1\n")

    effective = load_effective_configuration(
        tmp_path, environ={"SDAI_ORG_POLICY_PATH": str(org_path)}
    )

    assert effective.environment_allowlist == frozenset()


def test_enterprise_org_environment_allowlist_can_only_be_narrowed(tmp_path: Path):
    _config(tmp_path)
    write_text(
        tmp_path / ".sdai" / "policy.yaml",
        """version: 1
execution:
  environment_allowlist: [HOME, OPENAI_API_KEY, GITHUB_TOKEN]
""",
    )
    org_path = _external_policy(
        tmp_path,
        "org-env",
        """version: 1
execution:
  environment_allowlist: [HOME, OPENAI_API_KEY]
""",
    )

    effective = load_effective_configuration(
        tmp_path, environ={"SDAI_ORG_POLICY_PATH": str(org_path)}
    )

    assert effective.environment_allowlist == frozenset({"HOME", "OPENAI_API_KEY"})


def test_provider_environment_policy_gates_credential_discovery_and_network_config(
    monkeypatch: pytest.MonkeyPatch,
):
    values = {
        "PATH": "safe-path",
        "HOME": "/credential-home",
        "USERPROFILE": "C:/credential-home",
        "APPDATA": "C:/credential-home/AppData/Roaming",
        "LOCALAPPDATA": "C:/credential-home/AppData/Local",
        "XDG_CONFIG_HOME": "/credential-home/.config",
        "HTTPS_PROXY": "https://proxy.example",
        "HTTP_PROXY": "http://proxy.example",
        "NO_PROXY": "localhost",
        "SSL_CERT_FILE": "/enterprise/cert.pem",
        "REQUESTS_CA_BUNDLE": "/enterprise/ca.pem",
        "OPENAI_API_KEY": "provider-token",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    environment = build_provider_environment(
        "codex", policy_allowlist=frozenset({"OPENAI_API_KEY"})
    )

    assert environment["PATH"] == "safe-path"
    assert environment["OPENAI_API_KEY"] == "provider-token"
    for gated_name in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    ):
        assert gated_name not in environment


def test_individual_provider_environment_keeps_native_credential_discovery_by_default(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("HOME", "/developer-home")
    monkeypatch.setenv("OPENAI_API_KEY", "developer-token")

    environment = build_provider_environment("codex")

    assert environment["HOME"] == "/developer-home"
    assert environment["OPENAI_API_KEY"] == "developer-token"


def test_core_and_upper_layer_denies_cannot_be_weakened(tmp_path: Path):
    _config(tmp_path)
    write_text(
        tmp_path / ".sdai" / "policy.yaml",
        """version: 1
execution:
  workspace_write: true
  allow_force_approval_bypass: true
  protected_paths: []
skills:
  required:
    security: []
architecture_validation:
  allow_waivers: true
""",
    )
    org_path = _external_policy(
        tmp_path,
        "org-denies",
        """version: 1
execution:
  workspace_write: false
  allow_force_approval_bypass: false
skills:
  required:
    security: [enterprise-security]
architecture_validation:
  allow_waivers: false
""",
    )

    effective = load_effective_configuration(
        tmp_path, environ={"SDAI_ORG_POLICY_PATH": str(org_path)}
    )

    assert not effective.workspace_write
    assert not effective.allow_force_approval_bypass
    assert not effective.architecture_allow_waivers
    assert "enterprise-security" in effective.required_skills(Capability.SECURITY)
    assert set(CORE_PROTECTED_PATHS).issubset(effective.protected_paths)


def test_security_reference_does_not_regress_to_pre_019_claims():
    text = (Path(__file__).parents[1] / "docs" / "EXECUTION-SECURITY.md").read_text(
        encoding="utf-8"
    )
    lowered = text.lower()
    assert "v0.5.1" not in lowered
    assert "immutable audit/provenance is a future control" not in lowered
    assert "native credential store" in lowered
    assert "explicitly allow" in lowered
