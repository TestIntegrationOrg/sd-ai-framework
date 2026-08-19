from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor, AuditBinding
from sdai.trace_cli import main as trace_main


FEATURE = "AUDIT-COVER-239"


def _sha(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def test_generic_json_audit_decision_does_not_enter_typed_evidence_freshness(
    tmp_path: Path,
    capsys,
) -> None:
    _write(tmp_path / ".sdai" / "config.yaml", "version: 1\n")
    feature = tmp_path / "specs" / "changes" / FEATURE
    _write(feature / "requirements.md", "# Requirements\n\n- FR-001: Keep audit decisions inspectable.\n")
    decision = _write(
        feature / "quality" / "decision.json",
        json.dumps(
            {
                "apiVersion": "example.quality-decision/v1",
                "status": "passed",
                "decisionSha256": "sha256:" + "2" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )
    relative = decision.relative_to(tmp_path).as_posix()
    AuditLedger(tmp_path, FEATURE).append(
        category="evidence",
        actor=AuditActor("system", "coverage-test"),
        action=AuditAction("quality.decision.recorded", f"feature:{FEATURE}"),
        bindings=(AuditBinding("quality", relative, _sha(decision.read_bytes())),),
        metadata={"status": "recorded"},
    )

    code = trace_main(["coverage", FEATURE, "--json", "--path", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 2
    assert "error:" not in captured.out
    payload = json.loads(captured.out)
    assert payload["apiVersion"] == "sdai.trace-coverage/v1"
    assert payload["requirements_total"] == 1
    assert payload["requirements_uncovered"] == 1
