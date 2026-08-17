from __future__ import annotations

import json
from pathlib import Path

from sdai.contract_cli import main


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    _write(root / ".sdai" / "config.yaml", "{}\n")
    _write(
        root / ".sdai" / "contracts.yaml",
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: public-api
    kind: openapi
    path: contracts/openapi.yaml
""",
    )
    _write(root / "contracts" / "openapi.yaml", "openapi: 3.1.0\n")
    return root


def test_contract_inspect_accepts_path_after_subcommand_and_emits_json(
    tmp_path: Path,
    capsys,
) -> None:
    root = _project(tmp_path)
    assert main(["inspect", "--path", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "ContractInspection"
    assert payload["sources"][0]["source"]["id"] == "public-api"


def test_contract_check_without_adapter_fails_closed_as_json(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    assert main(["check", "public-api", "--path", str(root), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "SDAI-CONTRACT-ADAPTER-001"


def test_contract_cli_rejects_url_source_without_network_access(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    _write(
        root / ".sdai" / "contracts.yaml",
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: public-api
    kind: openapi
    path: https://example.invalid/openapi.yaml
""",
    )
    assert main(["inspect", "--path", str(root), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "SDAI-CONTRACT-SOURCE-002"
