from __future__ import annotations

import json
from pathlib import Path

import yaml

from sdai.entrypoint import main
from sdai.pack_certification import (
    CertificationRequirement,
    PackCertificationPolicy,
    PackEvalCaseResult,
    PackEvalEvidence,
    ProducerMetadata,
    resolve_pack_certification_policy,
)
from sdai.pack_integrity import build_pack_content_index
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, load_pack_manifest


def _project(root: Path) -> None:
    (root / ".sdai").mkdir(parents=True)
    (root / ".sdai" / "config.yaml").write_text("provider: mock\n", encoding="utf-8")


def _artifact(root: Path):
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "review.md").write_text("Review Δ requirements.\n", encoding="utf-8")
    raw = {
        "apiVersion": PACK_MANIFEST_API_VERSION,
        "id": "secure-coding",
        "publisher": "acme",
        "version": "1.2.3",
        "description": "Secure Δ Pack",
        "capabilities": ["skills"],
        "contentRoots": ["skills"],
        "dependencies": [],
        "compatibility": {
            "framework": ">=0.5.4,<1.0.0",
            "apis": ["sdai.pack-manifest/v1"],
        },
    }
    (root / "pack.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    manifest = load_pack_manifest(root / "pack.yaml")
    return manifest, build_pack_content_index(root, manifest)


def _policy():
    return PackCertificationPolicy(
        require_certification=True,
        default=CertificationRequirement(8000, (("quality", 8000),), ("quality-case",)),
    )


def test_certification_cli_reports_certified_json(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    artifact = tmp_path / "café-artifact"
    _project(project)
    manifest, content = _artifact(artifact)
    policy = _policy()
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(policy.to_json(), encoding="utf-8")
    resolved = resolve_pack_certification_policy(repository=policy)
    evidence = PackEvalEvidence(
        pack_identity=manifest.identity,
        manifest_sha256=manifest.sha256,
        content_sha256=content.sha256,
        policy_sha256=resolved.sha256,
        suite_sha256="sha256:" + "a" * 64,
        cases=(PackEvalCaseResult("quality-case", "quality", "sha256:" + "b" * 64, 9000, True),),
        producer=ProducerMetadata("mock", "deterministic-v1", "sdai-test"),
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(evidence.to_json(), encoding="utf-8")

    code = main([
        "pack", "certification",
        "--source", str(artifact),
        "--repository-policy", str(policy_path),
        "--evidence", str(evidence_path),
        "--json", "--path", str(project),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "certified"
    assert payload["certified"] is True
    assert payload["aggregateScoreBasisPoints"] == 9000


def test_certification_cli_missing_or_malformed_evidence_returns_four(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    artifact = tmp_path / "artifact"
    _project(project)
    _artifact(artifact)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(_policy().to_json(), encoding="utf-8")

    code = main([
        "pack", "certification", "--source", str(artifact),
        "--repository-policy", str(policy_path), "--json", "--path", str(project),
    ])
    captured = capsys.readouterr()
    assert code == 4
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == "missing"

    broken = tmp_path / "broken.json"
    broken.write_text('{"apiVersion":"broken"', encoding="utf-8")
    code = main([
        "pack", "certification", "--source", str(artifact),
        "--repository-policy", str(policy_path), "--evidence", str(broken),
        "--json", "--path", str(project),
    ])
    captured = capsys.readouterr()
    assert code == 4
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "malformed"
    assert payload["reasons"] == ["evidence-malformed"]


def test_certification_cli_detects_stale_evidence_after_policy_change(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    artifact = tmp_path / "artifact"
    _project(project)
    manifest, content = _artifact(artifact)
    old_policy = _policy()
    old_resolved = resolve_pack_certification_policy(repository=old_policy)
    evidence = PackEvalEvidence(
        pack_identity=manifest.identity,
        manifest_sha256=manifest.sha256,
        content_sha256=content.sha256,
        policy_sha256=old_resolved.sha256,
        suite_sha256="sha256:" + "c" * 64,
        cases=(PackEvalCaseResult("quality-case", "quality", "sha256:" + "d" * 64, 10000, True),),
        producer=ProducerMetadata("provider-one", "model-one", "runner"),
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(evidence.to_json(), encoding="utf-8")
    changed = PackCertificationPolicy(
        require_certification=True,
        default=CertificationRequirement(9500, (("quality", 9500),), ("quality-case",)),
    )
    changed_path = tmp_path / "changed.json"
    changed_path.write_text(changed.to_json(), encoding="utf-8")

    code = main([
        "pack", "certification", "--source", str(artifact),
        "--repository-policy", str(changed_path), "--evidence", str(evidence_path),
        "--json", "--path", str(project),
    ])
    captured = capsys.readouterr()

    assert code == 4
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "stale"
    assert "policy-changed" in payload["reasons"]
