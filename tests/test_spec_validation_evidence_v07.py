from __future__ import annotations

from pathlib import Path

import yaml

from sdai.spec_changes import load_current_spec
from sdai.spec_validation import parse_current_requirements, validate_spec_change


def _write_fixture(root: Path) -> tuple[Path, Path, Path]:
    current = root / "specs" / "current" / "signing" / "specification.md"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(
        """# Signing

## Functional Requirements
- FR-001: The service MUST sign a PowerShell file.

## Acceptance Criteria
- AC-001: A valid request returns a signed file.
""",
        encoding="utf-8",
    )
    loaded = load_current_spec(root, "signing")
    requirement_hash = parse_current_requirements(loaded).by_id()["FR-001"].sha256

    change_root = root / "specs" / "changes" / "SIGN-200"
    delta_root = change_root / "deltas"
    delta_root.mkdir(parents=True, exist_ok=True)
    change = change_root / "change.yaml"
    delta = delta_root / "signing.yaml"
    change.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "feature_id": "SIGN-200",
                "title": "Evidence-bound change",
                "status": "draft",
                "domains": ["signing"],
                "baselines": {"signing": loaded.sha256},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    delta.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "domain": "signing",
                "baseline_spec_sha256": loaded.sha256,
                "operations": [
                    {
                        "op": "MODIFIED",
                        "requirement_id": "FR-001",
                        "previous_hash": requirement_hash,
                        "definition": "The service MUST sign a PowerShell file using a trusted key.",
                        "reason": "Bind evidence to delta content.",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return current, change, delta


def test_validation_is_read_only_and_hashes_the_complete_change_bundle(tmp_path: Path) -> None:
    current, change, delta = _write_fixture(tmp_path)
    before = {path: path.read_bytes() for path in (current, change, delta)}

    first = validate_spec_change(tmp_path, "SIGN-200")

    assert first.valid is True
    assert first.change_sha256.startswith("sha256:")
    assert {path: path.read_bytes() for path in before} == before

    payload = yaml.safe_load(delta.read_text(encoding="utf-8"))
    payload["operations"][0]["reason"] = "Same behavior, different reviewed rationale."
    delta.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    second = validate_spec_change(tmp_path, "SIGN-200")

    assert second.valid is True
    assert second.change_sha256 != first.change_sha256
    assert current.read_bytes() == before[current]
    assert change.read_bytes() == before[change]
