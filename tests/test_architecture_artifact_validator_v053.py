from __future__ import annotations

from pathlib import Path

import yaml

from sdai.architecture_artifact_validator import (
    format_architecture_artifact_report,
    has_architecture_blockers,
    validate_architecture_artifacts,
)
from sdai.models import FeatureContext, LifecycleMode
from sdai.policy import EffectiveConfiguration, OperatingMode, load_effective_configuration
from sdai.scaffold import init_project
from sdai.v05_scaffold import install_v05_scaffold
from sdai.validation import validate


def _project(tmp_path: Path) -> FeatureContext:
    init_project(tmp_path)
    install_v05_scaffold(tmp_path)
    context = FeatureContext(tmp_path, "ARCH-900")
    context.feature_dir.mkdir(parents=True, exist_ok=True)
    return context


def _write(context: FeatureContext, relative: str, text: str) -> None:
    path = context.artifact(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _valid_critical_feature(context: FeatureContext) -> None:
    _write(
        context,
        "specification.md",
        "# Specification\n\nFR-001 Process payment.\nNFR-001 Retry safely.\nAC-001 Retry is observable.\n",
    )
    _write(
        context,
        "rfc/RFC-001-retry.md",
        "# RFC-001 Retry\n\nStatus: Draft\n\n## Problem\nTransient failures require governed retry behavior.\n",
    )
    _write(
        context,
        "architecture/architecture.md",
        "# Architecture\n\n## Option A - synchronous retry\nTrade-offs.\n\n## Option B - queue retry\nTrade-offs.\n",
    )
    _write(
        context,
        "architecture/decision-matrix.md",
        "# Decision Matrix\n\n| Option | Reliability | Cost |\n|---|---|---|\n| A | Medium | Low |\n| B | High | Medium |\n",
    )
    _write(
        context,
        "adr/ADR-001-retry.md",
        "# ADR-001 Retry\n\nStatus: Proposed\n\n## Context\nChoose a retry mechanism.\n",
    )
    _write(
        context,
        "architecture/diagrams/context.puml",
        "@startuml\nactor User\nrectangle System\nUser --> System\n@enduml\n",
    )
    _write(
        context,
        "architecture/diagrams/component.drawio",
        "<mxfile><diagram><mxGraphModel><root><mxCell id=\"0\"/><mxCell id=\"1\" parent=\"0\"/><mxCell id=\"2\" value=\"Service\" vertex=\"1\" parent=\"1\"/></root></mxGraphModel></diagram></mxfile>\n",
    )
    _write(
        context,
        "architecture/diagrams/retry-sequence.puml",
        "@startuml\nactor User\nparticipant API\nUser -> API: request\n@enduml\n",
    )
    _write(
        context,
        "security/threat-model.md",
        "# Threat Model\n\nTrust boundary: client to API. Mitigation: authenticated requests and least privilege.\n",
    )
    _write(
        context,
        "contracts/openapi.yaml",
        "openapi: 3.1.0\ninfo:\n  title: Retry API\n  version: 1.0.0\npaths: {}\n",
    )
    _write(
        context,
        "tasks.yaml",
        "tasks:\n  - id: TASK-1\n    title: Implement retry\n    traces_to: [FR-001, NFR-001, AC-001]\n",
    )


def test_scaffold_installs_architecture_validation_profile(tmp_path: Path):
    _project(tmp_path)
    config = yaml.safe_load(
        (tmp_path / ".sdai" / "architecture-validation.yaml").read_text(encoding="utf-8")
    )
    assert "rfc" in config["modes"]["critical"]["required"]
    assert "c4-context" in config["modes"]["critical"]["required"]
    assert "api-event-contracts" in config["modes"]["critical"]["required"]
    assert config["settings"]["critical_waiver_requires_approval"] is True


def test_critical_feature_reports_missing_architecture_lifecycle_artifacts(tmp_path: Path):
    context = _project(tmp_path)
    findings = validate_architecture_artifacts(context, LifecycleMode.CRITICAL)
    missing = {f.requirement for f in findings if f.code == "ARCH_ARTIFACT_MISSING"}
    assert {
        "specification",
        "rfc",
        "architecture-alternatives",
        "decision-matrix",
        "adr",
        "c4-context",
        "component-diagram",
        "sequence-diagram",
        "security-model",
        "api-event-contracts",
        "traceability",
    }.issubset(missing)
    assert has_architecture_blockers(findings)


def test_valid_critical_feature_satisfies_architecture_artifact_validator(tmp_path: Path):
    context = _project(tmp_path)
    _valid_critical_feature(context)
    findings = validate_architecture_artifacts(context, LifecycleMode.CRITICAL)
    assert not [f for f in findings if f.level == "ERROR"]
    assert {f.requirement for f in findings if f.code == "ARCH_ARTIFACT_OK"} == {
        "specification",
        "rfc",
        "architecture-alternatives",
        "decision-matrix",
        "adr",
        "c4-context",
        "component-diagram",
        "sequence-diagram",
        "security-model",
        "api-event-contracts",
        "traceability",
    }


def test_invalid_diagram_and_contract_are_blocking(tmp_path: Path):
    context = _project(tmp_path)
    _valid_critical_feature(context)
    _write(context, "architecture/diagrams/retry-sequence.puml", "participant API\n")
    _write(context, "contracts/openapi.yaml", "info:\n  title: not-a-contract\n")

    findings = validate_architecture_artifacts(context, LifecycleMode.CRITICAL)
    invalid = {f.requirement for f in findings if f.code == "ARCH_ARTIFACT_INVALID"}
    assert "sequence-diagram" in invalid
    assert "api-event-contracts" in invalid


def test_critical_waiver_requires_reason_and_approver(tmp_path: Path):
    context = _project(tmp_path)
    _valid_critical_feature(context)
    context.artifact("contracts/openapi.yaml").unlink()
    _write(
        context,
        "architecture/validation-waivers.yaml",
        "version: 1\nwaivers:\n  api-event-contracts:\n    reason: Internal algorithm only; no API or event contract changes.\n",
    )
    findings = validate_architecture_artifacts(context, LifecycleMode.CRITICAL)
    assert any(
        f.requirement == "api-event-contracts" and f.level == "ERROR" for f in findings
    )

    _write(
        context,
        "architecture/validation-waivers.yaml",
        "version: 1\nwaivers:\n  api-event-contracts:\n    reason: Internal algorithm only; no API or event contract changes.\n    approved_by: architecture-review\n",
    )
    findings = validate_architecture_artifacts(context, LifecycleMode.CRITICAL)
    assert any(
        f.requirement == "api-event-contracts" and f.code == "ARCH_ARTIFACT_WAIVED"
        for f in findings
    )


def test_enterprise_policy_can_add_requirement_and_disable_waivers(tmp_path: Path, monkeypatch):
    context = _project(tmp_path)
    _valid_critical_feature(context)

    config_path = tmp_path / ".sdai" / "architecture-validation.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["requirements"]["data-flow"] = {
        "description": "Sensitive-data flow diagram",
        "any_of": ["architecture/diagrams/data-flow.puml"],
        "check": "diagram",
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    org = tmp_path.parent / f"{tmp_path.name}-organization-policy.yaml"
    org.write_text(
        "version: 1\n"
        "providers: {}\n"
        "capabilities: {}\n"
        "execution: {}\n"
        "skills: {}\n"
        "architecture_validation:\n"
        "  required:\n"
        "    critical: [data-flow]\n"
        "  allow_waivers: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SDAI_ORG_POLICY_PATH", str(org.resolve()))

    effective = load_effective_configuration(tmp_path)
    assert effective.required_architecture_artifacts("critical") == ("data-flow",)
    assert effective.architecture_allow_waivers is False

    findings = validate_architecture_artifacts(
        context,
        LifecycleMode.CRITICAL,
        effective_configuration=effective,
    )
    assert any(f.requirement == "data-flow" and f.level == "ERROR" for f in findings)


def test_effective_configuration_constructor_remains_backward_compatible():
    effective = EffectiveConfiguration(
        operating_mode=OperatingMode.INDIVIDUAL,
        sources=(),
        allowed_profiles=None,
        allowed_providers=None,
        allowed_models={},
        capability_profiles={},
        capability_providers={},
        workspace_write=True,
        require_prior_approval_for_workspace_write=False,
        allow_force_approval_bypass=True,
        protected_paths=(),
        environment_allowlist=None,
        required_skills_map={},
    )

    assert effective.required_architecture_artifacts("critical") == ()
    assert effective.architecture_allow_waivers is True


def test_main_validation_includes_architecture_artifact_blockers(tmp_path: Path):
    context = _project(tmp_path)
    _write(context, "00-intake.md", "# Intake\n\nCritical feature intake.\n")
    findings = validate(context, LifecycleMode.CRITICAL)
    codes = {f.code for f in findings}
    assert "ARCH_ARTIFACT_MISSING" in codes


def test_report_renders_pass_fail_matrix(tmp_path: Path):
    context = _project(tmp_path)
    _valid_critical_feature(context)
    findings = validate_architecture_artifacts(context, LifecycleMode.CRITICAL)
    report = format_architecture_artifact_report(context.feature_id, LifecycleMode.CRITICAL, findings)
    assert "PASS  specification" in report
    assert "PASS  c4-context" in report
    assert "Result: PASS" in report

    context.artifact("rfc/RFC-001-retry.md").unlink()
    findings = validate_architecture_artifacts(context, LifecycleMode.CRITICAL)
    report = format_architecture_artifact_report(context.feature_id, LifecycleMode.CRITICAL, findings)
    assert "FAIL  rfc" in report
    assert "Result: BLOCKED" in report
