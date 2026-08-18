from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from sdai.contract_trace import build_contract_trace_index
from sdai.trace_builder import TraceGap, build_feature_trace_graph
from sdai.trace_cli import _coverage_payload
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceNodeType, TraceProvenance, TraceRelation
from sdai.verify_engine import _gap_finding


FEATURE = "CONTRACT-TRACE-208"
COMMIT = "a" * 40


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _feature(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """# Requirements

- FR-001: Preserve the public contract.
""",
    )
    _write(
        feature / "tasks.md",
        """# Tasks

- [ ] TASK-001: Implement FR-001.
""",
    )
    _write(
        feature / "tests.md",
        """# Tests

- TEST-001: Verify FR-001.
""",
    )
    _write(
        feature / "adr" / "ADR-001.md",
        """# ADR-001: Contract compatibility
status: accepted

ADR-001 governs FR-001.
""",
    )
    _write(
        feature / "approvals" / "architecture.yaml",
        """approval_id: APPROVAL-001
status: approved
references: [ADR-001]
""",
    )
    return feature


def _contracts(root: Path) -> None:
    _write(
        root / ".sdai" / "contracts.yaml",
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: public-api
    kind: openapi
    path: contracts/public-api.yaml
  - id: events
    kind: asyncapi
    path: contracts/events.yaml
  - id: profile-schema
    kind: json-schema
    path: contracts/profile.schema.json
  - id: users-proto
    kind: protobuf
    path: contracts/users.proto
""",
    )
    _write(
        root / "contracts" / "public-api.yaml",
        """openapi: 3.1.0
info:
  title: Public API
  version: 1.0.0
paths:
  /pets:
    get:
      operationId: listPets
      responses:
        '200':
          description: ok
""",
    )
    _write(
        root / "contracts" / "events.yaml",
        """asyncapi: 2.6.0
info:
  title: Events
  version: 1.0.0
channels:
  users/signed:
    publish:
      message:
        name: UserSigned
        payload:
          type: object
components:
  messages:
    Audit:
      payload:
        type: object
""",
    )
    _write(
        root / "contracts" / "profile.schema.json",
        """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "name": {"type": "string"}
  }
}
""",
    )
    _write(
        root / "contracts" / "users.proto",
        """syntax = "proto3";
package demo;

message User {
  string id = 1;
}

service Users {
  rpc Get(User) returns (User);
}
""",
    )


def _symbol(index, source_id: str, address: str):
    return index.symbols[(source_id, address)]


def _trace_manifest(
    feature: Path,
    *,
    links: list[dict[str, object]],
) -> None:
    payload = {
        "apiVersion": "sdai.contract-trace/v1",
        "kind": "ContractTrace",
        "links": links,
    }
    _write(feature / "contract-trace.yaml", json.dumps(payload, indent=2) + "\n")


def _link(
    *,
    source_id: str,
    source_sha256: str,
    target: str,
    address: str | None = None,
    symbol_sha256: str | None = None,
    decision: dict[str, str] | None = None,
) -> dict[str, object]:
    contract: dict[str, object] = {"sourceId": source_id}
    if address is not None:
        contract["address"] = address
    result: dict[str, object] = {
        "contract": contract,
        "target": target,
        "sourceSha256": source_sha256,
    }
    if symbol_sha256 is not None:
        result["symbolSha256"] = symbol_sha256
    if decision is not None:
        result["decision"] = decision
    return result


def _evidence(feature: Path, subject: str) -> None:
    record = TraceEvidence(
        evidence_id="EVIDENCE-CONTRACT-001",
        kind=EvidenceKind.TEST,
        status=EvidenceStatus.PASSED,
        subject=subject,
        git_commit=COMMIT,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.SOURCE,
                "contracts/public-api.yaml",
                "sha256:" + "1" * 64,
            ),
        ),
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/evidence/contract-test.json",
                1,
                detail="contract test evidence",
            ),
        ),
        producer=EvidenceProducer("tester", "codex", "model-a"),
        result={"passed": 1, "failed": 0},
        command=("python", "-m", "pytest"),
        tool="pytest",
    )
    _write(feature / "evidence" / "contract-test.json", record.to_json())


def _decision(candidate_sha256: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "apiVersion": "sdai.contract-policy-decision/v1",
        "kind": "ContractPolicyDecision",
        "criticality": "critical",
        "changeClass": "breaking",
        "outcome": "allowed",
        "allowed": True,
        "baselineSha256": "sha256:" + "2" * 64,
        "candidateSha256": candidate_sha256,
        "diffSha256": "sha256:" + "3" * 64,
        "policySha256": "sha256:" + "4" * 64,
        "constitutionSha256": "sha256:" + "5" * 64,
        "requiredEvidence": ["architecture-approval", "migration-plan"],
        "classifications": [],
        "evidence": [],
        "reasons": [],
    }
    payload["sha256"] = _hash_json(payload)
    return payload


def test_cross_format_contract_symbols_have_stable_addresses_and_hashes(tmp_path: Path) -> None:
    _contracts(tmp_path)

    first = build_contract_trace_index(tmp_path)
    second = build_contract_trace_index(tmp_path)

    assert not first.gaps
    assert [node.as_dict() for node in first.nodes] == [node.as_dict() for node in second.nodes]
    assert set(first.sources) == {"public-api", "events", "profile-schema", "users-proto"}
    assert _symbol(first, "public-api", "/paths/~1pets/get").symbol_kind == "operation"
    assert _symbol(first, "events", "/channels/users~1signed").symbol_kind == "channel"
    assert _symbol(first, "events", "/channels/users~1signed/publish").symbol_kind == "operation"
    assert _symbol(first, "events", "/channels/users~1signed/publish/message").symbol_kind == "message"
    assert _symbol(first, "profile-schema", "#").symbol_kind == "schema"
    assert _symbol(first, "profile-schema", "#/properties/name").symbol_kind == "schema"
    assert _symbol(first, "users-proto", "/messages/.demo.User").symbol_kind == "message"
    assert _symbol(first, "users-proto", "/messages/.demo.User/fields/1").symbol_kind == "field"
    assert _symbol(first, "users-proto", "/services/.demo.Users").symbol_kind == "service"
    assert _symbol(first, "users-proto", "/services/.demo.Users/rpcs/Get").symbol_kind == "rpc"
    assert all(item.symbol_sha256.startswith("sha256:") for item in first.symbols.values())


def test_builder_links_contract_symbol_to_requirement_test_approval_and_evidence(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    _contracts(tmp_path)
    index = build_contract_trace_index(tmp_path)
    symbol = _symbol(index, "public-api", "/paths/~1pets/get")
    source_sha = index.sources["public-api"].metadata["source_sha256"]
    assert isinstance(source_sha, str)
    _evidence(feature, symbol.node_id)
    _trace_manifest(
        feature,
        links=[
            _link(
                source_id="public-api",
                source_sha256=source_sha,
                address=symbol.address,
                symbol_sha256=symbol.symbol_sha256,
                target="requirement:FR-001",
            ),
            _link(
                source_id="public-api",
                source_sha256=source_sha,
                address=symbol.address,
                symbol_sha256=symbol.symbol_sha256,
                target="test:TEST-001",
            ),
            _link(
                source_id="public-api",
                source_sha256=source_sha,
                address=symbol.address,
                symbol_sha256=symbol.symbol_sha256,
                target="approval:APPROVAL-001",
            ),
        ],
    )

    result = build_feature_trace_graph(tmp_path, FEATURE, environ={})
    edges = {(edge.relation, edge.source, edge.target) for edge in result.graph.edges}

    assert (TraceRelation.REFERENCES, symbol.node_id, "requirement:FR-001") in edges
    assert (TraceRelation.VERIFIED_BY, symbol.node_id, "test:TEST-001") in edges
    assert (TraceRelation.APPROVED_BY, symbol.node_id, "approval:APPROVAL-001") in edges
    assert (TraceRelation.EVIDENCED_BY, symbol.node_id, "evidence:EVIDENCE-CONTRACT-001") in edges
    explicit = [
        edge
        for edge in result.graph.edges
        if edge.source == symbol.node_id and edge.metadata.get("contract_trace_role") == "link"
    ]
    assert len(explicit) == 3
    assert all(edge.metadata["source_sha256"] == source_sha for edge in explicit)
    assert all(edge.metadata["symbol_sha256"] == symbol.symbol_sha256 for edge in explicit)


def test_stale_symbol_and_missing_target_are_deterministic_trace_gaps(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    _contracts(tmp_path)
    old = build_contract_trace_index(tmp_path)
    old_symbol = _symbol(old, "public-api", "/paths/~1pets/get")

    _write(
        tmp_path / "contracts" / "public-api.yaml",
        """openapi: 3.1.0
info:
  title: Public API
  version: 1.0.0
paths:
  /pets:
    get:
      operationId: listPetsV2
      responses:
        '200':
          description: ok
""",
    )
    current = build_contract_trace_index(tmp_path)
    current_symbol = _symbol(current, "public-api", "/paths/~1pets/get")
    current_source = current.sources["public-api"].metadata["source_sha256"]
    assert isinstance(current_source, str)
    assert current_symbol.symbol_sha256 != old_symbol.symbol_sha256
    _trace_manifest(
        feature,
        links=[
            _link(
                source_id="public-api",
                source_sha256=current_source,
                address=current_symbol.address,
                symbol_sha256=old_symbol.symbol_sha256,
                target="requirement:FR-001",
            ),
            _link(
                source_id="events",
                source_sha256=str(current.sources["events"].metadata["source_sha256"]),
                address="/channels/users~1signed",
                symbol_sha256=_symbol(current, "events", "/channels/users~1signed").symbol_sha256,
                target="requirement:FR-999",
            ),
        ],
    )

    first = build_feature_trace_graph(tmp_path, FEATURE, environ={})
    second = build_feature_trace_graph(tmp_path, FEATURE, environ={})

    assert first.as_dict() == second.as_dict()
    assert any(gap.kind == "stale-contract-symbol" for gap in first.gaps)
    assert any(
        gap.kind == "missing-contract-trace-target" and gap.target == "requirement:FR-999"
        for gap in first.gaps
    )
    assert not any(
        edge.source == current_symbol.node_id and edge.target == "requirement:FR-001"
        for edge in first.graph.edges
    )


def test_policy_decision_hash_is_bound_into_edge_metadata_and_provenance(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    _contracts(tmp_path)
    index = build_contract_trace_index(tmp_path)
    symbol = _symbol(index, "public-api", "/paths/~1pets/get")
    source_sha = index.sources["public-api"].metadata["source_sha256"]
    assert isinstance(source_sha, str)
    decision = _decision(source_sha)
    decision_path = feature / "evidence" / "contract-policy-decision.json"
    _write(decision_path, json.dumps(decision, sort_keys=True, separators=(",", ":")) + "\n")
    _trace_manifest(
        feature,
        links=[
            _link(
                source_id="public-api",
                source_sha256=source_sha,
                address=symbol.address,
                symbol_sha256=symbol.symbol_sha256,
                target="adr:ADR-001",
                decision={
                    "path": decision_path.relative_to(tmp_path).as_posix(),
                    "sha256": str(decision["sha256"]),
                },
            )
        ],
    )

    result = build_feature_trace_graph(tmp_path, FEATURE, environ={})
    edge = next(
        edge
        for edge in result.graph.edges
        if edge.source == symbol.node_id and edge.target == "adr:ADR-001"
    )

    assert edge.metadata["decision_sha256"] == decision["sha256"]
    assert edge.metadata["diff_sha256"] == decision["diffSha256"]
    assert edge.metadata["policy_sha256"] == decision["policySha256"]
    assert any(item.source.endswith("contract-policy-decision.json") for item in edge.provenance)


def test_trace_coverage_json_projects_contract_symbols_links_and_gaps(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    _contracts(tmp_path)
    index = build_contract_trace_index(tmp_path)
    symbol = _symbol(index, "public-api", "/paths/~1pets/get")
    source_sha = index.sources["public-api"].metadata["source_sha256"]
    assert isinstance(source_sha, str)
    _trace_manifest(
        feature,
        links=[
            _link(
                source_id="public-api",
                source_sha256=source_sha,
                address=symbol.address,
                symbol_sha256=symbol.symbol_sha256,
                target="requirement:FR-001",
            )
        ],
    )

    result = build_feature_trace_graph(tmp_path, FEATURE, environ={})
    payload = _coverage_payload(result, {})
    contract = payload["contract_trace"]
    assert isinstance(contract, dict)

    assert contract["sources_total"] == 4
    assert contract["symbols_total"] >= 10
    assert contract["symbols_linked"] == 1
    assert contract["symbols_unlinked"] == contract["symbols_total"] - 1
    assert contract["links"] == 1
    assert contract["gaps"] == 0
    linked_rows = [item for item in contract["symbols"] if item["linked"]]
    assert len(linked_rows) == 1
    assert linked_rows[0]["address"] == "/paths/~1pets/get"


def test_verify_gap_projection_preserves_contract_staleness_in_json_metadata() -> None:
    gap = TraceGap(
        kind="stale-contract-symbol",
        source=f"specs/changes/{FEATURE}/contract-trace.yaml",
        line=1,
        source_node_id="contract:symbol:public-api:sha256:" + "1" * 64,
        target="public-api:/paths/~1pets/get",
        relation="references",
        detail="symbol hash changed",
    )

    finding = _gap_finding(gap)
    payload = finding.as_dict()

    assert payload["metadata"]["gap_kind"] == "stale-contract-symbol"
    assert payload["metadata"]["target"] == gap.target
    assert payload["metadata"]["relation"] == "references"
