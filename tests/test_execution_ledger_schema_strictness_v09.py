from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.execution_ledger import ExecutionLedgerError, create_execution_run, load_execution_run


FEATURE = "LEDGER-SCHEMA"
BASELINE = "d" * 40


def _ledger(root: Path):
    feature = root / "specs" / FEATURE
    feature.mkdir(parents=True)
    (feature / "00-intake.md").write_text("# schema strictness\n", encoding="utf-8")
    return create_execution_run(root, FEATURE, "enterprise", BASELINE, run_id="run-schema")


def test_binding_source_must_remain_json_string_not_numeric_coercion(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    event = json.loads(ledger.events_path.read_text(encoding="utf-8").splitlines()[0])
    event["bindings"][0]["source"] = 123
    body = dict(event)
    body.pop("sha256")
    # The body hash is intentionally left stale: strict schema validation must reject
    # the field type before a numeric source could become the string path "123".
    ledger.events_path.write_text(json.dumps(event, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(ExecutionLedgerError, match="binding.*string|binding source"):
        ledger.load_events()


def test_run_manifest_feature_and_workflow_fields_must_be_strings(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    payload = json.loads(ledger.manifest_path.read_text(encoding="utf-8"))
    payload["feature_id"] = 123
    ledger.manifest_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ExecutionLedgerError, match="feature_id.*string|run.json feature"):
        load_execution_run(tmp_path, FEATURE, "run-schema")


def test_event_recorded_at_must_be_string_not_datetime_like_integer(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    event = json.loads(ledger.events_path.read_text(encoding="utf-8").splitlines()[0])
    event["recorded_at"] = 20260812
    ledger.events_path.write_text(json.dumps(event, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(ExecutionLedgerError, match="recorded_at.*string|timestamp"):
        ledger.load_events()
