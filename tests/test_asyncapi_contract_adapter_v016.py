from __future__ import annotations

from pathlib import Path

from sdai.asyncapi_contracts import AsyncAPIContractAdapter
from sdai.contracts import (
    CompatibilityDirection,
    ContractAdapterRegistry,
    ContractSource,
    check_contract,
    diff_contracts,
    load_contract_snapshot,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _snapshot(root: Path, name: str, text: str):
    relative = f"contracts/{name}.yaml"
    _write(root / relative, text)
    return load_contract_snapshot(root, ContractSource(source_id=name, kind="asyncapi", path=relative))


def _doc(channels: str, components: str = "") -> str:
    return f"""asyncapi: 2.6.0
info:
  title: Events
  version: 1.0.0
channels:
{channels}
{components}"""


def _registry() -> ContractAdapterRegistry:
    return ContractAdapterRegistry([AsyncAPIContractAdapter()])


def test_valid_asyncapi_and_local_ref_are_accepted(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        "events",
        _doc(
            """  user.created:
    publish:
      message:
        $ref: '#/components/messages/UserCreated'
""",
            """components:
  messages:
    UserCreated:
      payload:
        type: object
        required: [id]
        properties:
          id: {type: string}
""",
        ),
    )
    assert check_contract(snapshot, _registry()).valid


def test_malformed_and_unsupported_asyncapi_are_rejected(tmp_path: Path) -> None:
    malformed = _snapshot(tmp_path, "bad", "asyncapi: [\n")
    assert [item.code for item in check_contract(malformed, _registry()).findings] == [
        "SDAI-CONTRACT-ASYNCAPI-001"
    ]
    unsupported = _snapshot(
        tmp_path,
        "old",
        "asyncapi: 1.2.0\ninfo: {title: x, version: '1'}\nchannels: {}\n",
    )
    assert "SDAI-CONTRACT-ASYNCAPI-003" in {
        item.code for item in check_contract(unsupported, _registry()).findings
    }


def test_external_and_unresolved_refs_fail_closed(tmp_path: Path) -> None:
    external = _snapshot(
        tmp_path,
        "external",
        _doc(
            """  events:
    publish:
      message:
        $ref: https://example.invalid/message.yaml
"""
        ),
    )
    assert "SDAI-CONTRACT-ASYNCAPI-007" in {
        item.code for item in check_contract(external, _registry()).findings
    }
    missing = _snapshot(
        tmp_path,
        "missing",
        _doc(
            """  events:
    publish:
      message:
        $ref: '#/components/messages/Missing'
"""
        ),
    )
    assert "SDAI-CONTRACT-ASYNCAPI-008" in {
        item.code for item in check_contract(missing, _registry()).findings
    }


def test_channel_and_operation_removal_are_breaking(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        _doc(
            """  user.created:
    publish:
      message:
        payload: {type: object}
  user.deleted:
    subscribe:
      message:
        payload: {type: object}
"""
        ),
    )
    after = _snapshot(
        tmp_path,
        "after",
        _doc(
            """  user.created:
    publish:
      message:
        payload: {type: object}
"""
        ),
    )
    result = diff_contracts(before, after, _registry())
    codes = {item.code for item in result.findings}
    assert "SDAI-CONTRACT-ASYNCAPI-DIFF-001" in codes
    assert "SDAI-CONTRACT-ASYNCAPI-DIFF-002" in codes
    assert not result.compatible


def test_payload_required_type_and_enum_changes_are_breaking(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        _doc(
            """  user.changed:
    publish:
      message:
        payload:
          type: object
          properties:
            state: {type: string, enum: [active, inactive]}
            count: {type: integer}
"""
        ),
    )
    after = _snapshot(
        tmp_path,
        "after",
        _doc(
            """  user.changed:
    publish:
      message:
        payload:
          type: object
          required: [state]
          properties:
            state: {type: string, enum: [active]}
            count: {type: string}
"""
        ),
    )
    codes = {item.code for item in diff_contracts(before, after, _registry()).findings}
    assert {
        "SDAI-CONTRACT-ASYNCAPI-DIFF-021",
        "SDAI-CONTRACT-ASYNCAPI-DIFF-022",
        "SDAI-CONTRACT-ASYNCAPI-DIFF-023",
    } <= codes


def test_message_and_operation_binding_changes_are_breaking(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        _doc(
            """  events:
    publish:
      bindings:
        kafka: {groupId: {type: string}}
      message:
        bindings:
          kafka: {key: {type: string}}
        payload: {type: object}
"""
        ),
    )
    after = _snapshot(
        tmp_path,
        "after",
        _doc(
            """  events:
    publish:
      bindings:
        kafka: {groupId: {type: integer}}
      message:
        bindings:
          kafka: {key: {type: integer}}
        payload: {type: object}
"""
        ),
    )
    codes = {item.code for item in diff_contracts(before, after, _registry()).findings}
    assert "SDAI-CONTRACT-ASYNCAPI-DIFF-004" in codes
    assert "SDAI-CONTRACT-ASYNCAPI-DIFF-005" in codes


def test_additive_operation_is_backward_compatible_but_forward_breaking(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        _doc(
            """  events:
    publish:
      message:
        payload: {type: object}
"""
        ),
    )
    after = _snapshot(
        tmp_path,
        "after",
        _doc(
            """  events:
    publish:
      message:
        payload: {type: object}
    subscribe:
      message:
        payload: {type: object}
"""
        ),
    )
    backward = diff_contracts(before, after, _registry(), CompatibilityDirection.BACKWARD)
    forward = diff_contracts(before, after, _registry(), CompatibilityDirection.FORWARD)
    full = diff_contracts(before, after, _registry(), CompatibilityDirection.FULL)
    assert backward.compatible
    assert not forward.compatible
    assert not full.compatible
    assert all(item.compatibility is CompatibilityDirection.FORWARD for item in forward.findings)


def test_asyncapi_3_top_level_operation_and_output_are_byte_stable(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        """asyncapi: 3.0.0
info:
  title: Events
  version: 1.0.0
channels:
  userEvents:
    address: user.events
    messages:
      UserEvent:
        payload: {type: object}
operations:
  sendUserEvent:
    action: send
    channel:
      $ref: '#/channels/userEvents'
    messages:
      - $ref: '#/channels/userEvents/messages/UserEvent'
""",
    )
    after = _snapshot(
        tmp_path,
        "after",
        """asyncapi: 3.0.0
info:
  title: Events
  version: 1.0.0
channels:
  userEvents:
    address: user.events
    messages:
      UserEvent:
        payload: {type: object}
operations: {}
""",
    )
    left = diff_contracts(before, after, _registry())
    right = diff_contracts(before, after, _registry())
    assert not left.compatible
    assert left.to_json() == right.to_json()
