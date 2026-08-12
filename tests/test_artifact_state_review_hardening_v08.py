from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.artifact_state import (
    ArtifactEvidenceInput,
    ArtifactFreshness,
    ArtifactStateError,
    evaluate_artifact_states,
    record_artifact_state,
)


FEATURE = "STATE-HARDEN-1"


def _schema(root: Path, artifacts: list[dict[str, object]]) -> None:
    path = root / ".sdai" / "schemas" / "hardening.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "sdai/v1",
                "kind": "ArtifactSchema",
                "metadata": {"id": "state-hardening", "version": "1.0.0"},
                "spec": {"artifacts": artifacts},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def test_domain_scoped_artifacts_use_independent_state_records(tmp_path: Path) -> None:
    _schema(
        tmp_path,
        [
            {
                "id": "domain-contract",
                "path": "specs/changes/{feature}/domains/{domain}/contract.md",
                "type": "markdown",
                "depends_on": [],
                "applies_to": ["standard"],
            }
        ],
    )
    for domain in ("signing", "certificates"):
        _write(
            tmp_path / "specs" / "changes" / FEATURE / "domains" / domain / "contract.md",
            f"# {domain}\n\ncontract for {domain}\n",
        )
        record_artifact_state(
            tmp_path,
            FEATURE,
            "domain-contract",
            domain=domain,
            environ={},
        )

    state_dir = tmp_path / "specs" / "changes" / FEATURE / ".sdai" / "artifact-state"
    assert (state_dir / "domain-contract--signing.yaml").is_file()
    assert (state_dir / "domain-contract--certificates.yaml").is_file()

    signing = evaluate_artifact_states(
        tmp_path,
        FEATURE,
        domain="signing",
        environ={},
    ).by_id()["domain-contract"]
    certificates = evaluate_artifact_states(
        tmp_path,
        FEATURE,
        domain="certificates",
        environ={},
    ).by_id()["domain-contract"]

    assert signing.freshness is ArtifactFreshness.FRESH
    assert certificates.freshness is ArtifactFreshness.FRESH
    assert signing.record_source != certificates.record_source


def test_dependency_hash_field_must_be_actual_mapping(tmp_path: Path) -> None:
    _schema(
        tmp_path,
        [
            {
                "id": "root-only",
                "path": "specs/changes/{feature}/root-only.md",
                "type": "markdown",
                "depends_on": [],
                "applies_to": ["standard"],
            }
        ],
    )
    _write(
        tmp_path / "specs" / "changes" / FEATURE / "root-only.md",
        "# root\n",
    )
    record = record_artifact_state(
        tmp_path,
        FEATURE,
        "root-only",
        environ={},
    )
    path = tmp_path / record.source
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    for malformed in ([], False, 0, ""):
        payload["dependency_sha256"] = malformed
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        with pytest.raises(
            ArtifactStateError,
            match="SDAI-STATE-002.*dependency_sha256 must be a mapping",
        ):
            evaluate_artifact_states(tmp_path, FEATURE, environ={})


def test_directory_hash_framing_detects_layout_change_that_old_separator_scheme_collided(
    tmp_path: Path,
) -> None:
    _schema(
        tmp_path,
        [
            {
                "id": "bundle",
                "path": "specs/changes/{feature}/bundle",
                "type": "directory",
                "depends_on": [],
                "applies_to": ["standard"],
            }
        ],
    )
    bundle = tmp_path / "specs" / "changes" / FEATURE / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "a").write_bytes(b"x\nb\0y")
    record_artifact_state(tmp_path, FEATURE, "bundle", environ={})

    (bundle / "a").write_bytes(b"x")
    (bundle / "b").write_bytes(b"y")

    state = evaluate_artifact_states(tmp_path, FEATURE, environ={}).by_id()["bundle"]
    assert state.freshness is ArtifactFreshness.STALE
    assert state.current_sha256 != state.recorded_sha256


@pytest.mark.parametrize(
    "source",
    [
        "/absolute/evidence.yaml",
        "C:/evidence.yaml",
        r"specs\\changes\\evidence.yaml",
        "specs/../evidence.yaml",
        "specs/bad?.yaml",
        "specs//evidence.yaml",
    ],
)
def test_new_evidence_binding_requires_portable_repository_relative_path(
    tmp_path: Path,
    source: str,
) -> None:
    _schema(
        tmp_path,
        [
            {
                "id": "root-only",
                "path": "specs/changes/{feature}/root-only.md",
                "type": "markdown",
                "depends_on": [],
                "applies_to": ["standard"],
            }
        ],
    )
    _write(
        tmp_path / "specs" / "changes" / FEATURE / "root-only.md",
        "# root\n",
    )

    with pytest.raises(ArtifactStateError, match="SDAI-STATE-004.*evidence source"):
        record_artifact_state(
            tmp_path,
            FEATURE,
            "root-only",
            evidence=(ArtifactEvidenceInput("approval", "approval", source),),
            environ={},
        )


def test_malformed_persisted_evidence_source_fails_closed(tmp_path: Path) -> None:
    _schema(
        tmp_path,
        [
            {
                "id": "root-only",
                "path": "specs/changes/{feature}/root-only.md",
                "type": "markdown",
                "depends_on": [],
                "applies_to": ["standard"],
            }
        ],
    )
    _write(
        tmp_path / "specs" / "changes" / FEATURE / "root-only.md",
        "# root\n",
    )
    evidence = _write(
        tmp_path / "specs" / "changes" / FEATURE / "approval.yaml",
        "status: approved\n",
    )
    record = record_artifact_state(
        tmp_path,
        FEATURE,
        "root-only",
        evidence=(
            ArtifactEvidenceInput(
                "approval",
                "approval",
                evidence.relative_to(tmp_path).as_posix(),
            ),
        ),
        environ={},
    )
    path = tmp_path / record.source
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["evidence"][0]["source"] = "/absolute/evidence.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        ArtifactStateError,
        match="SDAI-STATE-002.*evidence #1 source.*repository-relative",
    ):
        evaluate_artifact_states(tmp_path, FEATURE, environ={})
