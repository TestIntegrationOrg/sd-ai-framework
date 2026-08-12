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
    promote_spec_change,
    record_promotion_approval,
)
from sdai.spec_validation import parse_current_requirements


def _write_policy(root: Path, allowed: list[str] | None = None) -> None:
    path = root / ".sdai" / "approval-policies.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "gates": {
                    "spec-promotion": {
                        "min_approvals": 1,
                        "required_roles": [],
                        "allowed_approvers": allowed or [],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_current(root: Path, domain: str, definition: str) -> Path:
    path = root / "specs" / "current" / domain / "specification.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# {domain}

## Functional Requirements
- FR-001: {definition}

## Acceptance Criteria
- AC-001: A valid request satisfies {domain} behavior.
""",
        encoding="utf-8",
    )
    return path


def _requirement_hash(root: Path, domain: str) -> str:
    current = load_current_spec(root, domain)
    return parse_current_requirements(current).by_id()["FR-001"].sha256


def _write_change(
    root: Path,
    feature: str,
    domain_definitions: dict[str, str],
) -> None:
    change_root = root / "specs" / "changes" / feature
    delta_root = change_root / "deltas"
    delta_root.mkdir(parents=True, exist_ok=True)
    baselines = {
        domain: load_current_spec(root, domain).sha256
        for domain in domain_definitions
    }
    (change_root / "change.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "feature_id": feature,
                "title": f"Change {feature}",
                "status": "proposed",
                "domains": list(domain_definitions),
                "baselines": baselines,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for domain, definition in domain_definitions.items():
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
                            "reason": "Promotion hardening regression.",
                        }
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )


def test_current_allowlist_revocation_invalidates_previously_recorded_approval(
    tmp_path: Path,
) -> None:
    _write_policy(tmp_path, ["a@example.com"])
    current = _write_current(
        tmp_path,
        "signing",
        "The service MUST sign a PowerShell file.",
    )
    _write_change(
        tmp_path,
        "SIGN-REVOKE",
        {"signing": "The service MUST sign with an approved key."},
    )
    before = current.read_bytes()

    recorded = record_promotion_approval(
        tmp_path,
        "SIGN-REVOKE",
        approved_by="a@example.com",
    )
    assert recorded.satisfied is True

    _write_policy(tmp_path, ["b@example.com"])
    decision = evaluate_promotion_approval(tmp_path, "SIGN-REVOKE")

    assert decision.satisfied is False
    assert decision.approvals == 0
    assert decision.identities == ()
    assert "ignored disallowed=a@example.com" in decision.detail
    with pytest.raises(SpecPromotionError, match="SDAI-SPECPROMO-004"):
        promote_spec_change(tmp_path, "SIGN-REVOKE")
    assert current.read_bytes() == before


def test_concurrent_current_change_before_second_replace_aborts_and_preserves_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_policy(tmp_path)
    signing = _write_current(
        tmp_path,
        "signing",
        "The service MUST sign a PowerShell file.",
    )
    certificates = _write_current(
        tmp_path,
        "certificates",
        "The service MUST load a certificate chain.",
    )
    _write_change(
        tmp_path,
        "SIGN-RACE",
        {
            "signing": "The service MUST sign with an approved key.",
            "certificates": "The service MUST validate the certificate chain.",
        },
    )
    record_promotion_approval(
        tmp_path,
        "SIGN-RACE",
        approved_by="architect@example.com",
    )
    diff = build_spec_diff(tmp_path, "SIGN-RACE")
    targets = {
        item.domain: tmp_path / item.source
        for item in diff.domains
    }
    first_domain = diff.domains[0].domain
    second_domain = diff.domains[1].domain
    first_target = targets[first_domain].resolve()
    second_target = targets[second_domain].resolve()
    before_first = first_target.read_bytes()

    real_replace = promotion_module.os.replace
    injected = False
    concurrent_text = (
        second_target.read_text(encoding="utf-8")
        .replace("MUST", "MUST NOW", 1)
    )

    def replace_and_inject(src, dst) -> None:
        nonlocal injected
        destination = Path(dst).resolve()
        real_replace(src, dst)
        if destination == first_target and not injected:
            second_target.write_text(concurrent_text, encoding="utf-8")
            injected = True

    monkeypatch.setattr(promotion_module.os, "replace", replace_and_inject)

    with pytest.raises(
        SpecPromotionError,
        match=rf"SDAI-SPECPROMO-005.*domain '{second_domain}'.*changed before replacement",
    ):
        promote_spec_change(tmp_path, "SIGN-RACE")

    assert injected is True
    assert first_target.read_bytes() == before_first
    assert second_target.read_text(encoding="utf-8") == concurrent_text
    assert (tmp_path / "specs" / "changes" / "SIGN-RACE").is_dir()
    assert signing.exists() and certificates.exists()


def test_feature_named_archive_promotes_into_separate_archive_namespace(
    tmp_path: Path,
) -> None:
    _write_policy(tmp_path)
    _write_current(
        tmp_path,
        "signing",
        "The service MUST sign a PowerShell file.",
    )
    _write_change(
        tmp_path,
        "archive",
        {"signing": "The service MUST sign with an approved key."},
    )
    record_promotion_approval(
        tmp_path,
        "archive",
        approved_by="architect@example.com",
    )

    result = promote_spec_change(tmp_path, "archive")

    assert result.archive_path.startswith("specs/archive/changes/archive/")
    archive_path = tmp_path / result.archive_path
    assert archive_path.is_dir()
    assert (archive_path / "promotion.yaml").is_file()
    assert not (tmp_path / "specs" / "changes" / "archive").exists()
