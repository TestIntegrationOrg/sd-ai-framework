from __future__ import annotations

from pathlib import Path

from sdai.contracts import (
    CompatibilityDirection,
    ContractSource,
    check_contract,
    diff_contracts,
    load_contract_snapshot,
)
from sdai.protobuf_contracts import ProtobufContractAdapter


def _snapshot(root: Path, name: str, text: str, *, path: str | None = None):
    relative = path or f"contracts/{name}.proto"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return load_contract_snapshot(
        root,
        ContractSource(source_id=name, kind="protobuf", path=relative),
    )


def _adapter(*sources):
    return ProtobufContractAdapter(sources)


def test_common_proto_surface_validates_deterministically(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        "api",
        """
syntax = "proto3";
package demo.v1;

message User {
  string id = 1;
  repeated string roles = 2;
  oneof contact {
    string email = 3;
    string phone = 4;
  }
  reserved 9 to 10, "legacy";
}

enum State {
  STATE_UNSPECIFIED = 0;
  ACTIVE = 1;
}

service Users {
  rpc Get (User) returns (User);
  rpc Watch (User) returns (stream User);
}
""",
    )
    adapter = _adapter(snapshot)
    left = check_contract(snapshot, type("Registry", (), {"resolve": lambda self, kind: adapter})())
    right = check_contract(snapshot, type("Registry", (), {"resolve": lambda self, kind: adapter})())
    assert left.valid
    assert left.to_json() == right.to_json()
    assert any(
        item.code == "SDAI-CONTRACT-PROTOBUF-000" and "proto3" in item.message
        for item in left.findings
    )


def test_malformed_and_duplicate_surfaces_fail_closed(tmp_path: Path) -> None:
    malformed = _snapshot(tmp_path, "malformed", 'syntax = "proto3"; message User { string id = 1;')
    result = check_contract(
        malformed,
        type("Registry", (), {"resolve": lambda self, kind: _adapter(malformed)})(),
    )
    assert not result.valid
    assert "SDAI-CONTRACT-PROTOBUF-001" in {item.code for item in result.findings}

    duplicate = _snapshot(
        tmp_path,
        "duplicate",
        """
syntax = "proto3";
message User {
  string id = 1;
  string other = 1;
}
""",
    )
    result = check_contract(
        duplicate,
        type("Registry", (), {"resolve": lambda self, kind: _adapter(duplicate)})(),
    )
    assert not result.valid
    assert "SDAI-CONTRACT-PROTOBUF-005" in {item.code for item in result.findings}


def test_explicit_relative_import_resolves_without_filesystem_search(tmp_path: Path) -> None:
    common = _snapshot(
        tmp_path,
        "common",
        'syntax = "proto3"; package demo; message Common { string id = 1; }',
        path="contracts/common.proto",
    )
    api = _snapshot(
        tmp_path,
        "api",
        'syntax = "proto3"; package demo; import "common.proto"; message Api { Common value = 1; }',
        path="contracts/api.proto",
    )
    result = check_contract(
        api,
        type("Registry", (), {"resolve": lambda self, kind: _adapter(api, common)})(),
    )
    assert result.valid


def test_imports_fail_closed_when_unsafe_unresolved_or_ambiguous(tmp_path: Path) -> None:
    cases = [
        ("unsafe", 'import "../common.proto";', "SDAI-CONTRACT-PROTOBUF-007", ()),
        ("remote", 'import "https://example.invalid/common.proto";', "SDAI-CONTRACT-PROTOBUF-007", ()),
        ("missing", 'import "common.proto";', "SDAI-CONTRACT-PROTOBUF-008", ()),
    ]
    for name, import_line, code, dependencies in cases:
        api = _snapshot(
            tmp_path,
            name,
            f'syntax = "proto3"; {import_line} message Api {{ string id = 1; }}',
            path=f"contracts/{name}.proto",
        )
        adapter = _adapter(api, *dependencies)
        result = check_contract(
            api,
            type("Registry", (), {"resolve": lambda self, kind, adapter=adapter: adapter})(),
        )
        assert not result.valid
        assert code in {item.code for item in result.findings}

    exact = _snapshot(
        tmp_path,
        "exact",
        'syntax = "proto3";',
        path="common.proto",
    )
    relative = _snapshot(
        tmp_path,
        "relative",
        'syntax = "proto3";',
        path="contracts/common.proto",
    )
    ambiguous = _snapshot(
        tmp_path,
        "ambiguous",
        'syntax = "proto3"; import "common.proto"; message Api { string id = 1; }',
        path="contracts/ambiguous.proto",
    )
    adapter = _adapter(ambiguous, exact, relative)
    result = check_contract(
        ambiguous,
        type("Registry", (), {"resolve": lambda self, kind: adapter})(),
    )
    assert not result.valid
    assert "SDAI-CONTRACT-PROTOBUF-009" in {item.code for item in result.findings}


def test_field_number_reuse_type_and_cardinality_changes_are_breaking(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        """
syntax = "proto3";
package demo;
message User {
  string id = 1;
  string name = 2;
  repeated string roles = 3;
}
""",
    )
    after = _snapshot(
        tmp_path,
        "after",
        """
syntax = "proto3";
package demo;
message User {
  int64 account_id = 1;
  int32 name = 2;
  string roles = 3;
}
""",
    )
    registry = type(
        "Registry",
        (),
        {"resolve": lambda self, kind: _adapter(before, after)},
    )()
    result = diff_contracts(before, after, registry)
    codes = {item.code for item in result.findings}
    assert not result.compatible
    assert "SDAI-CONTRACT-PROTOBUF-DIFF-004" in codes
    assert "SDAI-CONTRACT-PROTOBUF-DIFF-005" in codes


def test_removed_field_requires_number_reservation(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        'syntax = "proto3"; message User { string id = 1; string legacy = 2; }',
    )
    unsafe = _snapshot(
        tmp_path,
        "unsafe",
        'syntax = "proto3"; message User { string id = 1; }',
    )
    safe = _snapshot(
        tmp_path,
        "safe",
        'syntax = "proto3"; message User { string id = 1; reserved 2, "legacy"; }',
    )
    unsafe_registry = type(
        "Registry",
        (),
        {"resolve": lambda self, kind: _adapter(before, unsafe)},
    )()
    safe_registry = type(
        "Registry",
        (),
        {"resolve": lambda self, kind: _adapter(before, safe)},
    )()
    unsafe_result = diff_contracts(before, unsafe, unsafe_registry)
    safe_result = diff_contracts(before, safe, safe_registry)
    assert not unsafe_result.compatible
    assert "SDAI-CONTRACT-PROTOBUF-DIFF-002" in {
        item.code for item in unsafe_result.findings
    }
    assert safe_result.compatible


def test_reopening_reserved_surface_and_adding_required_field_are_breaking(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        """
syntax = "proto2";
message User {
  optional string id = 1;
  reserved 9, "legacy";
}
""",
    )
    after = _snapshot(
        tmp_path,
        "after",
        """
syntax = "proto2";
message User {
  optional string id = 1;
  required string name = 2;
}
""",
    )
    registry = type(
        "Registry",
        (),
        {"resolve": lambda self, kind: _adapter(before, after)},
    )()
    result = diff_contracts(before, after, registry)
    codes = {item.code for item in result.findings}
    assert "SDAI-CONTRACT-PROTOBUF-DIFF-006" in codes
    assert "SDAI-CONTRACT-PROTOBUF-DIFF-005" in codes


def test_enum_service_and_rpc_changes_are_classified(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        """
syntax = "proto3";
package demo;
message Request { string id = 1; }
message Response { string id = 1; }
enum State { UNKNOWN = 0; ACTIVE = 1; }
service Users {
  rpc Get (Request) returns (Response);
  rpc Watch (Request) returns (stream Response);
}
""",
    )
    after = _snapshot(
        tmp_path,
        "after",
        """
syntax = "proto3";
package demo;
message Request { string id = 1; }
message Response { string id = 1; }
enum State { UNKNOWN = 0; }
service Users {
  rpc Get (stream Request) returns (Response);
}
""",
    )
    registry = type(
        "Registry",
        (),
        {"resolve": lambda self, kind: _adapter(before, after)},
    )()
    result = diff_contracts(before, after, registry)
    codes = {item.code for item in result.findings}
    assert "SDAI-CONTRACT-PROTOBUF-DIFF-008" in codes
    assert "SDAI-CONTRACT-PROTOBUF-DIFF-011" in codes
    assert "SDAI-CONTRACT-PROTOBUF-DIFF-012" in codes


def test_message_enum_service_package_syntax_and_public_import_removal(tmp_path: Path) -> None:
    dep = _snapshot(tmp_path, "dep", 'syntax = "proto3";', path="contracts/dep.proto")
    before = _snapshot(
        tmp_path,
        "before",
        """
syntax = "proto3";
package oldpkg;
import public "dep.proto";
message Removed { string id = 1; }
enum Gone { ZERO = 0; }
service Old { rpc Call (Removed) returns (Removed); }
""",
        path="contracts/api.proto",
    )
    after = _snapshot(
        tmp_path,
        "after",
        """
syntax = "proto2";
package newpkg;
import "dep.proto";
""",
        path="candidate/api.proto",
    )
    registry = type(
        "Registry",
        (),
        {"resolve": lambda self, kind: _adapter(before, dep)},
    )()
    result = diff_contracts(before, after, registry)
    codes = {item.code for item in result.findings}
    assert {
        "SDAI-CONTRACT-PROTOBUF-DIFF-001",
        "SDAI-CONTRACT-PROTOBUF-DIFF-007",
        "SDAI-CONTRACT-PROTOBUF-DIFF-010",
        "SDAI-CONTRACT-PROTOBUF-DIFF-013",
        "SDAI-CONTRACT-PROTOBUF-DIFF-014",
        "SDAI-CONTRACT-PROTOBUF-DIFF-015",
    } <= codes


def test_additive_change_is_backward_compatible_but_forward_breaking(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        'syntax = "proto3"; message User { string id = 1; }',
    )
    after = _snapshot(
        tmp_path,
        "after",
        'syntax = "proto3"; message User { string id = 1; string name = 2; }',
    )
    registry = type(
        "Registry",
        (),
        {"resolve": lambda self, kind: _adapter(before, after)},
    )()
    backward = diff_contracts(before, after, registry, CompatibilityDirection.BACKWARD)
    forward = diff_contracts(before, after, registry, CompatibilityDirection.FORWARD)
    full = diff_contracts(before, after, registry, CompatibilityDirection.FULL)
    assert backward.compatible
    assert not forward.compatible
    assert not full.compatible


def test_candidate_uses_baseline_logical_path_for_declared_relative_imports(tmp_path: Path) -> None:
    dep = _snapshot(
        tmp_path,
        "common",
        'syntax = "proto3"; message Common { string id = 1; }',
        path="contracts/common.proto",
    )
    before = _snapshot(
        tmp_path,
        "before",
        'syntax = "proto3"; import "common.proto"; message Api { Common value = 1; }',
        path="contracts/api.proto",
    )
    after = _snapshot(
        tmp_path,
        "after",
        'syntax = "proto3"; import "common.proto"; message Api { Common value = 1; string name = 2; }',
        path="candidate/api.proto",
    )
    registry = type(
        "Registry",
        (),
        {"resolve": lambda self, kind: _adapter(before, dep)},
    )()
    result = diff_contracts(before, after, registry)
    assert result.compatible
    assert "SDAI-CONTRACT-PROTOBUF-008" not in {item.code for item in result.findings}
