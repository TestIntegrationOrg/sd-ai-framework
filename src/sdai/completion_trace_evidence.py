from __future__ import annotations

from pathlib import Path

from sdai.execution_ledger import ExecutionLedger, HashBinding


class CompletionTraceEvidenceError(RuntimeError):
    pass


def evidence_binding(
    ledger: ExecutionLedger,
    path: Path,
) -> HashBinding:
    candidate = path if path.is_absolute() else ledger.project_root / path
    return ledger.binding_for_file(candidate, kind="evidence")
