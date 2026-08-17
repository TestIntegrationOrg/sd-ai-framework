from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.contracts import (
    CompatibilityDirection,
    ContractAdapterRegistry,
    ContractError,
    ContractFinding,
    ContractProvenance,
    ContractSeverity,
    check_contract,
    diff_contracts,
    discover_contracts,
    find_contract_source,
    load_contract_sources,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _project(tmp_path: Path, *, order: tuple[str, ...] = ("zeta", "alpha")) -> Path:
    root = tmp_path / "project"
    _write(root / ".sdai" / "config.yaml", "{}\n")
    items = {
        "alpha": ("openapi", "contracts/api.yaml"),
        "zeta": ("protobuf", "contracts/events.proto"),
    }
    source_lines = []
    for source_id in order:
        kind, path = items[source_id]
        source_lines.extend(
            [
                f"  - id: {source_id}",
                f"    kind: {kind}",
                f"    path: {path}",
            ]
        )
    _write(
        root / ".sdai" / "contracts.yaml",
        "\n".join(
            [
                "apiVersion: sdai.contract-sources/v1",
                "kind: ContractSources",
                "sources:",
                *source_lines,
                "",
            ]
        ),
    )
    _write(root / "contracts" / "api.yaml", "openapi: 3.1.0\ninfo:\n  title: Example\n")
    _write(root / "contracts" / "events.proto", 'syntax = "proto3";\nmessage Event {}\n')
    return root


def test_discovery_is_order_independent_and_byte_stable(tmp_path: Path) -> None:
    first = _project(tmp_path / "a", order=("zeta", "alpha"))
    second = _project(tmp_path / "b", order=("alpha", "zeta"))
    left = discover_contracts(first)
    right = discover_contracts(second)
    assert [item.source.source_id for item in left.sources] == ["alpha", "zeta"]
    assert left.to_json() == right.to_json()
    assert left.sha256 == right.sha256
    assert left.manifest_sha256 == right.manifest_sha256


def test_source_hash_normalizes_bom_and_newlines(tmp_path: Path) -> None:
    first = _project(tmp_path / "a")
    second = _project(tmp_path / "b")
    content = "\ufeffopenapi: 3.1.0\r\ninfo:\r\n  title: Example\r\n"
    (second / "contracts" / "api.yaml").write_bytes(content.encode("utf-8"))
    assert find_contract_source(first, "alpha").sha256 == find_contract_source(second, "alpha").sha256


def test_duplicate_source_identity_fails_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    manifest = root / ".sdai" / "contracts.yaml"
    manifest.write_text(
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: same
    kind: openapi
    path: contracts/api.yaml
  - id: same
    kind: protobuf
    path: contracts/events.proto
""",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="SDAI-CONTRACT-SOURCE-008"):
        load_contract_sources(root)


def test_duplicate_yaml_key_fails_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(
        root / ".sdai" / "contracts.yaml",
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
kind: ContractSources
sources:
  - id: alpha
    kind: openapi
    path: contracts/api.yaml
""",
    )
    with pytest.raises(ContractError, match="SDAI-CONTRACT-SOURCE-001"):
        load_contract_sources(root)


def test_unsupported_contract_kind_has_stable_error(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(
        root / ".sdai" / "contracts.yaml",
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: alpha
    kind: graphql
    path: contracts/api.yaml
""",
    )
    with pytest.raises(ContractError) as captured:
        load_contract_sources(root)
    assert captured.value.code == "SDAI-CONTRACT-SOURCE-004"
    assert json.loads(captured.value.to_json())["error"]["code"] == "SDAI-CONTRACT-SOURCE-004"


def test_absolute_parent_and_url_like_paths_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    for unsafe in ("../outside.yaml", "/tmp/outside.yaml", "https://example.invalid/api.yaml", "C:/temp/api.yaml"):
        _write(
            root / ".sdai" / "contracts.yaml",
            f"""apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: alpha
    kind: openapi
    path: {unsafe}
""",
        )
        with pytest.raises(ContractError) as captured:
            load_contract_sources(root)
        assert captured.value.code == "SDAI-CONTRACT-SOURCE-002"


def test_source_loading_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr("sdai.contracts.CONTRACT_SOURCE_MAX_BYTES", 8)
    with pytest.raises(ContractError) as captured:
        find_contract_source(root, "alpha")
    assert captured.value.code == "SDAI-CONTRACT-SOURCE-006"


def test_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "contracts" / "api.yaml").write_bytes(b"openapi: \xff\n")
    with pytest.raises(ContractError) as captured:
        find_contract_source(root, "alpha")
    assert captured.value.code == "SDAI-CONTRACT-SOURCE-005"


def test_symlink_sources_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    target = root / "contracts" / "real.yaml"
    _write(target, "openapi: 3.1.0\n")
    link = root / "contracts" / "api.yaml"
    link.unlink()
    try:
        link.symlink_to(target.name)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform/test environment")
    with pytest.raises(ContractError) as captured:
        find_contract_source(root, "alpha")
    assert captured.value.code == "SDAI-CONTRACT-SOURCE-007"


class _FakeAdapter:
    kind = "openapi"

    def check(self, snapshot):
        provenance = ContractProvenance(
            source_id=snapshot.source.source_id,
            source_path=snapshot.source.path,
            source_sha256=snapshot.sha256,
            pointer="/info/title",
        )
        return (
            ContractFinding("TEST-002", ContractSeverity.WARNING, "second", provenance=provenance),
            ContractFinding("TEST-001", ContractSeverity.INFO, "first", provenance=provenance),
        )

    def diff(self, before, after, direction):
        return (
            ContractFinding(
                "TEST-DIFF-001",
                ContractSeverity.WARNING,
                "changed",
                compatibility=direction,
                provenance=ContractProvenance(
                    source_id=after.source.source_id,
                    source_path=after.source.path,
                    source_sha256=after.sha256,
                ),
            ),
        )


def test_adapter_registry_fails_closed_and_rejects_ambiguity(tmp_path: Path) -> None:
    root = _project(tmp_path)
    snapshot = find_contract_source(root, "alpha")
    with pytest.raises(ContractError) as missing:
        check_contract(snapshot, ContractAdapterRegistry())
    assert missing.value.code == "SDAI-CONTRACT-ADAPTER-001"
    registry = ContractAdapterRegistry([_FakeAdapter()])
    with pytest.raises(ContractError) as duplicate:
        registry.register(_FakeAdapter())
    assert duplicate.value.code == "SDAI-CONTRACT-ADAPTER-002"


def test_adapter_results_and_diff_are_deterministic(tmp_path: Path) -> None:
    root = _project(tmp_path)
    before = find_contract_source(root, "alpha")
    _write(root / "contracts" / "next.yaml", "openapi: 3.1.0\ninfo:\n  title: Next\n")
    from sdai.contracts import load_explicit_snapshot

    after = load_explicit_snapshot(
        root,
        source_id="alpha",
        kind="openapi",
        path="contracts/next.yaml",
    )
    registry = ContractAdapterRegistry([_FakeAdapter()])
    checked = check_contract(before, registry)
    assert [finding.code for finding in checked.findings] == ["TEST-001", "TEST-002"]
    assert checked.to_json() == check_contract(before, registry).to_json()
    diffed = diff_contracts(before, after, registry, CompatibilityDirection.BACKWARD)
    assert diffed.findings[0].compatibility is CompatibilityDirection.BACKWARD
    assert diffed.to_json() == diff_contracts(before, after, registry, CompatibilityDirection.BACKWARD).to_json()
