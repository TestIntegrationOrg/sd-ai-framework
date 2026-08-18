from __future__ import annotations

import json
from pathlib import Path

from sdai.contract_adapters import default_contract_registry
from sdai.contract_cli import main
from sdai.contracts import ContractSource, diff_contracts, load_contract_snapshot
from sdai.protobuf_directional import ProtobufContractAdapter


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _snapshot(root: Path, source_id: str, path: str, text: str):
    _write(root / path, text)
    return load_contract_snapshot(
        root,
        ContractSource(source_id=source_id, kind="protobuf", path=path),
    )


def test_equivalent_relative_and_fully_qualified_types_do_not_break(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "api",
        "contracts/api.proto",
        """
syntax = "proto3";
package demo;
message User { string id = 1; }
message Envelope { User user = 1; }
service Users { rpc Get (User) returns (Envelope); }
""",
    )
    after = _snapshot(
        tmp_path,
        "candidate",
        "candidate/api.proto",
        """
syntax = "proto3";
package demo;
message User { string id = 1; }
message Envelope { .demo.User user = 1; }
service Users { rpc Get (.demo.User) returns (.demo.Envelope); }
""",
    )
    registry = default_contract_registry((before,))
    assert diff_contracts(before, after, registry).compatible


def test_equivalent_import_spellings_canonicalize_to_declared_source(tmp_path: Path) -> None:
    common = _snapshot(
        tmp_path,
        "common",
        "contracts/common.proto",
        'syntax = "proto3"; message Common { string id = 1; }',
    )
    before = _snapshot(
        tmp_path,
        "api",
        "contracts/api.proto",
        'syntax = "proto3"; import public "common.proto"; message Api { Common value = 1; }',
    )
    after = _snapshot(
        tmp_path,
        "candidate",
        "candidate/api.proto",
        'syntax = "proto3"; import public "contracts/common.proto"; message Api { Common value = 1; }',
    )
    registry = default_contract_registry((before, common))
    result = diff_contracts(before, after, registry)
    assert result.compatible
    assert "SDAI-CONTRACT-PROTOBUF-DIFF-015" not in {item.code for item in result.findings}


def test_cli_default_registry_receives_manifest_sources_for_import_resolution(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "project"
    _write(root / ".sdai" / "config.yaml", "{}\n")
    _write(
        root / ".sdai" / "contracts.yaml",
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: api
    kind: protobuf
    path: contracts/api.proto
  - id: common
    kind: protobuf
    path: contracts/common.proto
""",
    )
    _write(root / "contracts" / "common.proto", 'syntax = "proto3"; message Common { string id = 1; }')
    _write(
        root / "contracts" / "api.proto",
        'syntax = "proto3"; import "common.proto"; message Api { Common value = 1; }',
    )

    assert main(["check", "api", "--path", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["snapshot"]["source"]["id"] == "api"


def test_directional_adapter_is_provider_independent_and_deterministic(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        "contracts/before.proto",
        'syntax = "proto3"; message User { string id = 1; }',
    )
    after = _snapshot(
        tmp_path,
        "after",
        "contracts/after.proto",
        'syntax = "proto3"; message User { string id = 1; string name = 2; }',
    )
    adapter = ProtobufContractAdapter((before, after))
    left = adapter.diff(before, after, direction=__import__("sdai.contracts", fromlist=["CompatibilityDirection"]).CompatibilityDirection.BACKWARD)
    right = adapter.diff(before, after, direction=__import__("sdai.contracts", fromlist=["CompatibilityDirection"]).CompatibilityDirection.BACKWARD)
    assert tuple(item.to_dict() for item in left) == tuple(item.to_dict() for item in right)
