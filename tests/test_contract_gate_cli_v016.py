from __future__ import annotations

import json
from pathlib import Path

from sdai.constitution import init_constitution
from sdai.contract_cli import main


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _project(root: Path, baseline: str, candidate: str) -> Path:
    _write(root / ".sdai" / "config.yaml", "operating_mode: individual\n")
    init_constitution(root)
    _write(
        root / ".sdai" / "contracts.yaml",
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: public-schema
    kind: json-schema
    path: contracts/schema.json
""",
    )
    _write(root / "contracts" / "schema.json", baseline)
    _write(root / "candidate" / "schema.json", candidate)
    return root


def test_contract_gate_returns_policy_block_exit_two_with_deterministic_json(
    tmp_path: Path,
    capsys,
) -> None:
    root = _project(tmp_path / "project", '{"type":"string"}', '{"type":"integer"}')
    args = [
        "gate",
        "public-schema",
        "--against",
        "candidate/schema.json",
        "--criticality",
        "critical",
        "--path",
        str(root),
        "--json",
    ]
    assert main(args) == 2
    first = capsys.readouterr().out
    assert main(args) == 2
    second = capsys.readouterr().out
    assert first == second
    payload = json.loads(first)
    assert payload["apiVersion"] == "sdai.contract-policy-decision/v1"
    assert payload["changeClass"] == "breaking"
    assert payload["outcome"] == "blocked"
    assert payload["allowed"] is False
    assert payload["requiredEvidence"] == ["architecture-approval", "migration-plan"]


def test_contract_gate_allows_non_breaking_change_without_evidence(tmp_path: Path, capsys) -> None:
    root = _project(
        tmp_path / "project",
        '{"type":"string","enum":["active"]}',
        '{"type":["string","null"],"enum":["active",null]}',
    )
    assert (
        main(
            [
                "gate",
                "public-schema",
                "--against",
                "candidate/schema.json",
                "--criticality",
                "critical",
                "--path",
                str(root),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["changeClass"] == "non-breaking"
    assert payload["outcome"] == "allowed"
    assert payload["requiredEvidence"] == []


def test_contract_gate_malformed_policy_is_stable_error_exit_one(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path / "project", '{"type":"string"}', '{"type":"integer"}')
    _write(
        root / ".sdai" / "contract-policy.yaml",
        """apiVersion: sdai.contract-policy/v1
kind: ContractPolicy
rules:
  critical:
    allowUnknown: definitely-not-a-boolean
""",
    )
    assert (
        main(
            [
                "gate",
                "public-schema",
                "--against",
                "candidate/schema.json",
                "--path",
                str(root),
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "SDAI-CONTRACT-POLICY-001"


def test_contract_gate_unknown_contract_semantics_fail_closed(tmp_path: Path, capsys) -> None:
    root = _project(
        tmp_path / "project",
        '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"string"}',
        '{"$schema":"https://example.invalid/future-schema","type":"string"}',
    )
    assert (
        main(
            [
                "gate",
                "public-schema",
                "--against",
                "candidate/schema.json",
                "--criticality",
                "light",
                "--path",
                str(root),
                "--json",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["changeClass"] == "unknown"
    assert payload["outcome"] == "blocked"
