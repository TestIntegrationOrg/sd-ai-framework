from __future__ import annotations

from pathlib import Path

from sdai.contract_adapters import default_contract_registry
from sdai.contracts import CompatibilityDirection, ContractSource, diff_contracts, load_contract_snapshot


def _snapshot(root: Path, name: str, paths: str):
    relative = f"contracts/{name}.yaml"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"""openapi: 3.1.0
info:
  title: Direction API
  version: 1.0.0
paths:
{paths}
""",
        encoding="utf-8",
        newline="\n",
    )
    return load_contract_snapshot(
        root,
        ContractSource(source_id=name, kind="openapi", path=relative),
    )


def test_forward_and_backward_evaluate_opposite_directions(tmp_path: Path) -> None:
    before = _snapshot(
        tmp_path,
        "before",
        """  /pets:
    get:
      responses:
        '200': {description: ok}
""",
    )
    after = _snapshot(
        tmp_path,
        "after",
        """  /pets:
    get:
      responses:
        '200': {description: ok}
    post:
      responses:
        '201': {description: created}
""",
    )
    registry = default_contract_registry()

    backward = diff_contracts(before, after, registry, CompatibilityDirection.BACKWARD)
    forward = diff_contracts(before, after, registry, CompatibilityDirection.FORWARD)
    full = diff_contracts(before, after, registry, CompatibilityDirection.FULL)

    assert backward.compatible
    assert not forward.compatible
    assert any(item.compatibility is CompatibilityDirection.FORWARD for item in forward.findings)
    assert not full.compatible
    assert {item.compatibility for item in full.findings} == {CompatibilityDirection.FORWARD}
