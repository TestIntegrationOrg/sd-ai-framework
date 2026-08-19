from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.agent_platform import Capability, RoutingRequest, route_model
from sdai.agent_platform.routing_diagnostics import (
    RoutingDiagnosticError,
    load_routing_diagnostic,
    persist_routing_decision,
)
from sdai.scaffold import init_project


FEATURE = "ROUTING-DIAGNOSTIC-CANONICAL-020"


def test_routing_diagnostic_rejects_noncanonical_byte_rewrite(tmp_path: Path) -> None:
    init_project(tmp_path)
    feature = tmp_path / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-258-ROUTING: Preserve canonical routing evidence.\n",
        encoding="utf-8",
        newline="\n",
    )

    decision = route_model(
        tmp_path,
        RoutingRequest(semantic_role="developer", capability=Capability.CODING),
        environ={},
    )
    path = persist_routing_decision(tmp_path, FEATURE, decision)
    assert path is not None

    payload = json.loads(path.read_text(encoding="utf-8"))
    # Keep every logical field/hash unchanged but rewrite whitespace. A parsed-object
    # hash alone would accept this; immutable diagnostic evidence must require the
    # exact canonical JSON byte form as well.
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(RoutingDiagnosticError, match="bytes are not canonical"):
        load_routing_diagnostic(tmp_path, FEATURE, decision.sha256)
