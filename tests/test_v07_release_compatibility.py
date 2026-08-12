from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest
import yaml

import sdai.spec_promotion as promotion_module
from sdai.agent_platform.models import AgentInvocation
from sdai.agent_platform.skills import load_skill
from sdai.execution_excellence import load_execution_excellence_pack
from sdai.language_packs import TIER1_LANGUAGE_PACK_IDS, validate_tier1_language_packs
from sdai.orchestrator import Orchestrator
from sdai.skill_resolution import SkillResolutionError, resolve_skills
from sdai.spec_changes import load_current_spec
from sdai.spec_promotion import (
    SpecPromotionError,
    preview_promotion,
    promote_spec_change,
    record_promotion_approval,
)
from sdai.spec_validation import (
    detect_parallel_change_conflicts,
    parse_current_requirements,
    validate_spec_change,
)
from sdai.technology import detect_technologies
from sdai.version_entrypoint import main as sdai_main


def _write_current(
    root: Path,
    domain: str,
    *,
    functional: str,
    acceptance: str,
) -> Path:
    path = root / "specs" / "current" / domain / "specification.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# {domain}

Current truth note: preserve café/Δ evidence exactly.

## Functional Requirements
- FR-001: {functional}

## Non-Functional Requirements
- NFR-001: The service MUST emit correlated audit evidence.

## Acceptance Criteria
- AC-001: {acceptance}

## Notes
Unrelated Markdown must survive promotion.
""",
        encoding="utf-8",
    )
    return path


def _requirement_hash(root: Path, domain: str, requirement_id: str = "FR-001") -> str:
    current = load_current_spec(root, domain)
    return parse_current_requirements(current).by_id()[requirement_id].sha256


def _write_change(
    root: Path,
    feature: str,
    definitions: dict[str, str],
) -> None:
    change_root = root / "specs" / "changes" / feature
    delta_root = change_root / "deltas"
    delta_root.mkdir(parents=True, exist_ok=True)
    baselines = {
        domain: load_current_spec(root, domain).sha256
        for domain in definitions
    }
    (change_root / "change.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "feature_id": feature,
                "title": f"Release-gate change {feature}",
                "description": "SDAI 0.7 integrated promotion fixture.",
                "status": "proposed",
                "domains": list(definitions),
                "baselines": baselines,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for domain, definition in definitions.items():
        (delta_root / f"{domain}.yaml").write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "domain": domain,
                    "baseline_spec_sha256": baselines[domain],
                    "operations": [
                        {
                            "op": "MODIFIED",
                            "requirement_id": "FR-001",
                            "previous_hash": _requirement_hash(root, domain),
                            "definition": definition,
                            "reason": f"Apply approved {feature} behavior.",
                        }
                    ],
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )


def _minimal_project(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "operating_mode": "individual",
                "policy": {"repository": ".sdai/policy.yaml"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    agent = root / ".sdai" / "agents" / "developer.agent.md"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(
        """---
name: developer
description: Provider-neutral implementation role.
capabilities: [coding]
skills: []
execution_mode: advisory
providers: {}
---

Implement approved behavior without rewriting canonical truth.
""",
        encoding="utf-8",
    )


def _copy_skill(source_root: Path, target_root: Path, name: str) -> None:
    source = source_root / ".agents" / "skills" / name
    target = target_root / ".agents" / "skills" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)


def _write_java_21_only_skill(root: Path) -> None:
    skill_root = root / ".agents" / "skills" / "java-21-only"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        """---
name: java-21-only
description: Release-gate incompatible Java skill.
---

# Java 21 Only

Use only with an explicitly compatible Java baseline.
""",
        encoding="utf-8",
    )
    (skill_root / "sdai.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "capabilities": ["coding"],
                "compatible_agents": ["developer"],
                "requires": [],
                "compatibility": {"languages": {"java": ">=21"}},
                "selection": {"auto": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_v07_parallel_change_promotion_makes_competing_change_stale(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Enterprise Workspace Ω"
    root.mkdir()
    assert sdai_main(["init", "--path", str(root)]) == 0
    capsys.readouterr()

    current = _write_current(
        root,
        "signing",
        functional="The service MUST sign one PowerShell file.",
        acceptance="An approved request returns one signed file.",
    )
    before_current = current.read_bytes()
    _write_change(
        root,
        "SIGN-A",
        {"signing": "The service MUST sign one PowerShell file with an approved KMS key."},
    )
    _write_change(
        root,
        "SIGN-B",
        {"signing": "The service MUST sign one PowerShell file with a customer-selected key."},
    )

    assert validate_spec_change(root, "SIGN-A").valid is True
    assert validate_spec_change(root, "SIGN-B").valid is True
    conflicts = detect_parallel_change_conflicts(root)
    assert any(
        finding.code == "SDAI-SPECVAL-009"
        and set(finding.related_features) == {"SIGN-A", "SIGN-B"}
        for finding in conflicts.findings
    )

    delta = root / "specs" / "changes" / "SIGN-A" / "deltas" / "signing.yaml"
    before_delta = delta.read_bytes()
    preview = preview_promotion(root, "SIGN-A")
    assert preview.eligible is False
    assert current.read_bytes() == before_current
    assert delta.read_bytes() == before_delta
    assert not (root / "specs" / "changes" / "SIGN-A" / "promotion.yaml").exists()

    decision = record_promotion_approval(
        root,
        "SIGN-A",
        approved_by="architect@example.com",
        role="architect",
        note="Reviewed exact 0.7 release-gate diff.",
    )
    assert decision.satisfied is True
    result = promote_spec_change(root, "SIGN-A")

    promoted = load_current_spec(root, "signing")
    assert "approved KMS key" in promoted.content
    assert "café/Δ" in promoted.content
    assert "Unrelated Markdown must survive promotion." in promoted.content
    assert not (root / "specs" / "changes" / "SIGN-A").exists()
    archive = root / result.archive_path
    assert archive.is_dir()
    assert result.archive_path.startswith("specs/archive/changes/SIGN-A/")
    assert (archive / "promotion.yaml").is_file()

    stale = validate_spec_change(root, "SIGN-B")
    assert stale.valid is False
    assert {finding.code for finding in stale.findings}.intersection(
        {"SDAI-SPECVAL-003", "SDAI-SPECVAL-007"}
    )
    assert (root / "specs" / "changes" / "SIGN-B").is_dir()


def test_v07_multi_domain_promotion_failure_rolls_back_exact_preimages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Atomic Workspace café"
    root.mkdir()
    assert sdai_main(["init", "--path", str(root)]) == 0
    capsys.readouterr()

    signing = _write_current(
        root,
        "signing",
        functional="The service MUST sign one PowerShell file.",
        acceptance="A valid signing request succeeds.",
    )
    certificates = _write_current(
        root,
        "certificates",
        functional="The service MUST load the approved certificate chain.",
        acceptance="The approved certificate chain is available to signing.",
    )
    _write_change(
        root,
        "SIGN-ATOMIC",
        {
            "signing": "The service MUST sign one PowerShell file using the approved certificate chain.",
            "certificates": "The service MUST load and validate the approved certificate chain.",
        },
    )
    record_promotion_approval(
        root,
        "SIGN-ATOMIC",
        approved_by="architect@example.com",
        role="architect",
    )
    before = {
        signing.resolve(): signing.read_bytes(),
        certificates.resolve(): certificates.read_bytes(),
    }

    real_replace = promotion_module.os.replace
    current_targets = set(before)
    replacements = 0
    injected = False

    def fail_second_current_replace(src, dst) -> None:
        nonlocal replacements, injected
        destination = Path(dst).resolve()
        if destination in current_targets:
            replacements += 1
            if replacements == 2 and not injected:
                injected = True
                raise OSError("simulated release-gate second-domain failure")
        real_replace(src, dst)

    monkeypatch.setattr(promotion_module.os, "replace", fail_second_current_replace)

    with pytest.raises(
        SpecPromotionError,
        match="SDAI-SPECPROMO-007.*rolled back.*release-gate second-domain",
    ):
        promote_spec_change(root, "SIGN-ATOMIC")

    assert injected is True
    assert signing.read_bytes() == before[signing.resolve()]
    assert certificates.read_bytes() == before[certificates.resolve()]
    assert (root / "specs" / "changes" / "SIGN-ATOMIC").is_dir()
    assert not (root / "specs" / "archive" / "changes" / "SIGN-ATOMIC").exists()
    assert not (root / "specs" / "changes" / "SIGN-ATOMIC" / "promotion.yaml").exists()


def test_v07_tier1_assets_detect_and_resolve_without_language_specific_agents(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    packs = validate_tier1_language_packs(repository)
    execution = load_execution_excellence_pack(repository)

    assert tuple(pack.id for pack in packs) == TIER1_LANGUAGE_PACK_IDS
    assert execution.id == "sdai-execution-excellence"

    root = tmp_path / "Java Service Δ"
    root.mkdir()
    _minimal_project(root)
    for name in (
        "java-engineering",
        "spring-boot",
        "test-driven-development",
        "systematic-debugging",
        "verification-before-completion",
    ):
        _copy_skill(repository, root, name)
    _write_java_21_only_skill(root)
    (root / "pom.xml").write_text(
        """<project>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.4.10</version>
  </parent>
  <properties><java.version>17</java.version></properties>
</project>
""",
        encoding="utf-8",
    )

    technology = detect_technologies(root)
    facts = {(item.category, item.name): item for item in technology.technologies}
    assert facts[("languages", "java")].version == "17"
    assert facts[("frameworks", "spring-boot")].version == "3.4.10"
    assert all("\\" not in evidence.source for fact in facts.values() for evidence in fact.evidence)

    report = resolve_skills(
        root,
        agent_name="developer",
        capability="coding",
        task="implement feature change",
    )
    assert report.agent == "developer"
    assert report.selected == (
        "java-engineering",
        "spring-boot",
        "test-driven-development",
    )
    assert not any(
        report.agent.startswith(prefix)
        for prefix in ("java-", "codex-", "claude-", "copilot-", "gemini-")
    )

    with pytest.raises(
        SkillResolutionError,
        match="SDAI-SKILL-003.*java-21-only.*java version 17.*>=21",
    ):
        resolve_skills(
            root,
            agent_name="developer",
            capability="coding",
            requested=("java-21-only",),
        )


def test_v07_provider_override_and_upgrade_preserve_old_and_new_user_assets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Upgrade Compatibility Ω"
    root.mkdir()
    assert sdai_main(["init", "--path", str(root)]) == 0
    capsys.readouterr()

    result = Orchestrator(root).run_manual_step(
        "V07-PROVIDER",
        "enterprise",
        "architecture-review",
        dry_run=True,
        agent_override="architect",
        profile_override="codex",
    )
    assert result.status == "dry-run"
    assert isinstance(result.result, AgentInvocation)
    assert result.result.agent_name == "architect"
    assert result.result.profile.name == "codex"

    agent_path = root / ".sdai" / "agents" / "requirements-analyst.agent.md"
    customized_agent = agent_path.read_text(encoding="utf-8") + (
        "\nTeam customization: preserve 0.7 café/Δ review behavior.\n"
    )
    agent_path.write_text(customized_agent, encoding="utf-8")

    legacy = root / ".sdai" / "skills" / "LegacyReview"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "skill.yaml").write_text(
        "name: LegacyReview\ndescription: legacy compatibility skill\ncapabilities: [review]\n",
        encoding="utf-8",
    )
    (legacy / "SKILL.md").write_text(
        "# Legacy Review\n\nPreserve legacy café behavior.\n",
        encoding="utf-8",
    )

    technology_config = root / ".sdai" / "technology.yaml"
    technology_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "languages": {"java": "17"},
                "frameworks": {"spring-boot": "3.4.10"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    current = _write_current(
        root,
        "preserved-domain",
        functional="The system MUST preserve user-owned current truth.",
        acceptance="Upgrade leaves current truth byte-for-byte unchanged.",
    )
    before_technology = technology_config.read_bytes()
    before_current = current.read_bytes()

    assert sdai_main(["upgrade", "--path", str(root)]) == 0
    capsys.readouterr()

    assert agent_path.read_text(encoding="utf-8") == customized_agent
    assert "café behavior" in load_skill(root, "LegacyReview").instructions
    assert technology_config.read_bytes() == before_technology
    assert current.read_bytes() == before_current

    detected = detect_technologies(root)
    java = next(
        item
        for item in detected.technologies
        if item.category == "languages" and item.name == "java"
    )
    assert java.version == "17"
    assert java.version_source == "declared"

    assert sdai_main(["upgrade", "--path", str(root)]) == 0
    capsys.readouterr()
    assert agent_path.read_text(encoding="utf-8") == customized_agent
    assert technology_config.read_bytes() == before_technology
    assert current.read_bytes() == before_current
