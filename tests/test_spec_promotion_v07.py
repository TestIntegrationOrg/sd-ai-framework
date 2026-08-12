from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import sdai.spec_promotion as promotion_module
from sdai.spec_changes import load_current_spec
from sdai.spec_promotion import (
    SpecPromotionError,
    build_spec_diff,
    evaluate_promotion_approval,
    preview_promotion,
    promote_spec_change,
    record_promotion_approval,
)
from sdai.spec_validation import parse_current_requirements


def _current_text(*requirements: tuple[str, str], domain: str = "signing") -> str:
    lines = [
        f"# {domain}",
        "",
        "Introductory prose must survive promotion unchanged.",
        "",
        "## Functional Requirements",
    ]
    for requirement_id, definition in requirements:
        if requirement_id.startswith("FR-"):
            lines.append(f"- {requirement_id}: {definition}")
    lines.extend(["", "## Non-Functional Requirements"])
    for requirement_id, definition in requirements:
        if requirement_id.startswith("NFR-"):
            lines.append(f"- {requirement_id}: {definition}")
    lines.extend(["", "## Acceptance Criteria"])
    for requirement_id, definition in requirements:
        if requirement_id.startswith("AC-"):
            lines.append(f"- {requirement_id}: {definition}")
    lines.extend(["", "## Notes", "Keep this note exactly.", ""])
    return "\n".join(lines)


def _write_current(
    root: Path,
    domain: str = "signing",
    requirements: tuple[tuple[str, str], ...] = (
        ("FR-001", "The service MUST sign a PowerShell file."),
        ("FR-002", "The service MUST reject malformed input."),
        ("NFR-001", "Signing MUST complete within 2 seconds."),
        ("AC-001", "A valid request returns a signed file."),
    ),
) -> Path:
    path = root / "specs" / "current" / domain / "specification.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_current_text(*requirements, domain=domain), encoding="utf-8")
    return path


def _requirement_hash(root: Path, domain: str, requirement_id: str) -> str:
    current = load_current_spec(root, domain)
    return parse_current_requirements(current).by_id()[requirement_id].sha256


def _write_change(
    root: Path,
    feature: str,
    domain_operations: dict[str, list[dict[str, object]]],
    *,
    new_domains: set[str] | None = None,
) -> None:
    new_domains = new_domains or set()
    change_root = root / "specs" / "changes" / feature
    delta_root = change_root / "deltas"
    delta_root.mkdir(parents=True, exist_ok=True)
    baselines: dict[str, str | None] = {}
    for domain in domain_operations:
        baselines[domain] = (
            None if domain in new_domains else load_current_spec(root, domain).sha256
        )
    (change_root / "change.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "feature_id": feature,
                "title": f"Change {feature}",
                "description": "Promotion fixture.",
                "status": "proposed",
                "domains": list(domain_operations),
                "baselines": baselines,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for domain, operations in domain_operations.items():
        (delta_root / f"{domain}.yaml").write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "domain": domain,
                    "baseline_spec_sha256": baselines[domain],
                    "operations": operations,
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )


def _standard_operations(root: Path) -> list[dict[str, object]]:
    return [
        {
            "op": "MODIFIED",
            "requirement_id": "FR-001",
            "previous_hash": _requirement_hash(root, "signing", "FR-001"),
            "definition": "The service MUST sign a PowerShell file using an approved key.",
            "reason": "Clarify key trust.",
        },
        {
            "op": "REMOVED",
            "requirement_id": "FR-002",
            "previous_hash": _requirement_hash(root, "signing", "FR-002"),
            "reason": "Move malformed-input handling to a validation domain.",
        },
        {
            "op": "RENAMED",
            "requirement_id": "NFR-001",
            "new_requirement_id": "NFR-002",
            "previous_hash": _requirement_hash(root, "signing", "NFR-001"),
            "reason": "Align performance numbering.",
        },
        {
            "op": "ADDED",
            "requirement_id": "FR-003",
            "definition": "The service MUST preserve café/Δ metadata.",
            "reason": "Define UTF-8 behavior.",
        },
    ]


def test_semantic_diff_preserves_unrelated_markdown_and_applies_all_operations(
    tmp_path: Path,
) -> None:
    _write_current(tmp_path)
    _write_change(tmp_path, "SIGN-300", {"signing": _standard_operations(tmp_path)})

    report = build_spec_diff(tmp_path, "SIGN-300")
    domain = report.domains[0]

    assert domain.before_sha256 == load_current_spec(tmp_path, "signing").sha256
    assert domain.after_sha256 != domain.before_sha256
    assert "Introductory prose must survive promotion unchanged." in domain.proposed_content
    assert "Keep this note exactly." in domain.proposed_content
    assert "- FR-001: The service MUST sign a PowerShell file using an approved key." in domain.proposed_content
    assert "FR-002:" not in domain.proposed_content
    assert "- NFR-002: Signing MUST complete within 2 seconds." in domain.proposed_content
    assert "- FR-003: The service MUST preserve café/Δ metadata." in domain.proposed_content
    assert [item.op for item in domain.changes] == [
        "MODIFIED",
        "REMOVED",
        "RENAMED",
        "ADDED",
    ]
    assert domain.changes[-1].section == "Functional Requirements"


def test_new_domain_diff_creates_deterministic_requirement_sections(tmp_path: Path) -> None:
    _write_change(
        tmp_path,
        "SIGN-301",
        {
            "certificates": [
                {
                    "op": "ADDED",
                    "requirement_id": "FR-001",
                    "definition": "The service MUST load the approved certificate chain.",
                    "reason": "Create certificate domain.",
                },
                {
                    "op": "ADDED",
                    "requirement_id": "NFR-001",
                    "definition": "Certificate lookup MUST complete within 500 ms.",
                    "reason": "Define performance.",
                },
            ]
        },
        new_domains={"certificates"},
    )

    report = build_spec_diff(tmp_path, "SIGN-301")
    proposed = report.domains[0].proposed_content

    assert proposed.startswith("# certificates\n")
    assert "## Functional Requirements" in proposed
    assert "## Non-Functional Requirements" in proposed
    assert proposed.count("FR-001") == 1
    assert proposed.count("NFR-001") == 1


def test_invalid_change_cannot_generate_promotable_diff(tmp_path: Path) -> None:
    current_path = _write_current(tmp_path)
    _write_change(tmp_path, "SIGN-302", {"signing": _standard_operations(tmp_path)})
    before = current_path.read_bytes()
    current_path.write_text(
        current_path.read_text(encoding="utf-8").replace(
            "The service MUST sign a PowerShell file.",
            "The service MUST sign using newly changed current truth.",
        ),
        encoding="utf-8",
    )

    with pytest.raises(SpecPromotionError, match="SDAI-SPECPROMO-001"):
        build_spec_diff(tmp_path, "SIGN-302")

    assert current_path.read_bytes() != before
    assert (tmp_path / "specs" / "changes" / "SIGN-302").is_dir()


def test_promotion_requires_hash_bound_approval_and_delta_edit_stales_it(
    tmp_path: Path,
) -> None:
    current_path = _write_current(tmp_path)
    _write_change(tmp_path, "SIGN-303", {"signing": _standard_operations(tmp_path)})
    before = current_path.read_bytes()

    initial = evaluate_promotion_approval(tmp_path, "SIGN-303")
    assert initial.satisfied is False
    assert initial.stale is False

    with pytest.raises(SpecPromotionError, match="SDAI-SPECPROMO-004"):
        promote_spec_change(tmp_path, "SIGN-303")
    assert current_path.read_bytes() == before

    approved = record_promotion_approval(
        tmp_path,
        "SIGN-303",
        approved_by="architect@example.com",
        role="architect",
        note="Reviewed semantic diff.",
    )
    assert approved.satisfied is True
    first_hash = approved.change_sha256

    delta = tmp_path / "specs" / "changes" / "SIGN-303" / "deltas" / "signing.yaml"
    payload = yaml.safe_load(delta.read_text(encoding="utf-8"))
    payload["operations"][0]["reason"] = "Reviewed rationale changed after approval."
    delta.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    stale = evaluate_promotion_approval(tmp_path, "SIGN-303")
    assert stale.satisfied is False
    assert stale.stale is True
    assert stale.change_sha256 != first_hash
    assert current_path.read_bytes() == before


def test_approval_policy_enforces_allowed_approvers_required_roles_and_minimum(
    tmp_path: Path,
) -> None:
    _write_current(tmp_path)
    _write_change(tmp_path, "SIGN-304", {"signing": _standard_operations(tmp_path)})
    policy = tmp_path / ".sdai" / "approval-policies.yaml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "gates": {
                    "spec-promotion": {
                        "min_approvals": 2,
                        "required_roles": ["architect", "security"],
                        "allowed_approvers": ["a@example.com", "s@example.com"],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SpecPromotionError, match="not allowed"):
        record_promotion_approval(
            tmp_path,
            "SIGN-304",
            approved_by="other@example.com",
            role="architect",
        )
    with pytest.raises(SpecPromotionError, match="requires one of these roles"):
        record_promotion_approval(
            tmp_path,
            "SIGN-304",
            approved_by="a@example.com",
            role="developer",
        )

    one = record_promotion_approval(
        tmp_path,
        "SIGN-304",
        approved_by="a@example.com",
        role="architect",
    )
    assert one.satisfied is False
    assert one.missing_roles == ("security",)

    two = record_promotion_approval(
        tmp_path,
        "SIGN-304",
        approved_by="s@example.com",
        role="security",
    )
    assert two.satisfied is True
    assert two.approvals == 2
    assert two.missing_roles == ()


def test_preview_is_read_only_and_reports_approval_state(tmp_path: Path) -> None:
    current_path = _write_current(tmp_path)
    _write_change(tmp_path, "SIGN-305", {"signing": _standard_operations(tmp_path)})
    before_current = current_path.read_bytes()
    before_delta = (
        tmp_path / "specs" / "changes" / "SIGN-305" / "deltas" / "signing.yaml"
    ).read_bytes()

    preview = preview_promotion(tmp_path, "SIGN-305")

    assert preview.eligible is False
    assert preview.diff.domains[0].after_sha256 != preview.diff.domains[0].before_sha256
    assert current_path.read_bytes() == before_current
    assert (
        tmp_path / "specs" / "changes" / "SIGN-305" / "deltas" / "signing.yaml"
    ).read_bytes() == before_delta
    assert not (tmp_path / "specs" / "changes" / "SIGN-305" / "promotion.yaml").exists()


def test_successful_promotion_updates_truth_archives_change_and_records_evidence(
    tmp_path: Path,
) -> None:
    _write_current(tmp_path)
    _write_change(tmp_path, "SIGN-306", {"signing": _standard_operations(tmp_path)})
    preview = preview_promotion(tmp_path, "SIGN-306")
    record_promotion_approval(
        tmp_path,
        "SIGN-306",
        approved_by="architect@example.com",
        role="architect",
    )

    result = promote_spec_change(tmp_path, "SIGN-306")

    current = load_current_spec(tmp_path, "signing")
    assert current.sha256 == preview.diff.domains[0].after_sha256
    assert "FR-003" in current.content
    assert not (tmp_path / "specs" / "changes" / "SIGN-306").exists()
    archive = tmp_path / result.archive_path
    assert archive.is_dir()
    assert (archive / "change.yaml").is_file()
    assert (archive / "deltas" / "signing.yaml").is_file()
    evidence = yaml.safe_load((archive / "promotion.yaml").read_text(encoding="utf-8"))
    assert evidence["feature_id"] == "SIGN-306"
    assert evidence["change_sha256"] == result.change_sha256
    assert evidence["after_current_spec_sha256"]["signing"] == current.sha256
    assert evidence["approval"]["satisfied"] is True
    assert result.approved_by == ("architect@example.com",)


def test_transaction_failure_on_second_domain_restores_first_domain_byte_for_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signing = _write_current(tmp_path, "signing")
    certificates = _write_current(
        tmp_path,
        "certificates",
        requirements=(
            ("FR-001", "The service MUST load the certificate chain."),
            ("AC-001", "An approved chain is accepted."),
        ),
    )
    signing_ops = _standard_operations(tmp_path)
    certificate_ops = [
        {
            "op": "MODIFIED",
            "requirement_id": "FR-001",
            "previous_hash": _requirement_hash(tmp_path, "certificates", "FR-001"),
            "definition": "The service MUST load and validate the certificate chain.",
            "reason": "Add validation.",
        }
    ]
    _write_change(
        tmp_path,
        "SIGN-307",
        {"signing": signing_ops, "certificates": certificate_ops},
    )
    record_promotion_approval(
        tmp_path,
        "SIGN-307",
        approved_by="architect@example.com",
        role="architect",
    )
    before = {signing: signing.read_bytes(), certificates: certificates.read_bytes()}

    real_replace = promotion_module.os.replace
    current_targets = {
        signing.resolve(),
        certificates.resolve(),
    }
    calls = 0

    def fail_second_current_replace(src, dst) -> None:
        nonlocal calls
        destination = Path(dst).resolve()
        if destination in current_targets:
            calls += 1
            if calls == 2:
                raise OSError("simulated second-domain replace failure")
        real_replace(src, dst)

    monkeypatch.setattr(promotion_module.os, "replace", fail_second_current_replace)

    with pytest.raises(
        SpecPromotionError,
        match="SDAI-SPECPROMO-007.*rolled back.*simulated second-domain",
    ):
        promote_spec_change(tmp_path, "SIGN-307")

    assert signing.read_bytes() == before[signing]
    assert certificates.read_bytes() == before[certificates]
    assert (tmp_path / "specs" / "changes" / "SIGN-307").is_dir()
    assert not (tmp_path / "specs" / "changes" / "SIGN-307" / "promotion.yaml").exists()
