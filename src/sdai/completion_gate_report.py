from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from sdai.completion_gate import CompletionGateFinding
from sdai.completion_policy import CompletionRisk, CompletionScope


COMPLETION_GATE_REPORT_API_VERSION = "sdai.completion-gate-report/v1"


@dataclass(frozen=True)
class CompletionGateReport:
    feature_id: str
    run_id: str
    task_id: str
    attempt: int
    risk: CompletionRisk
    scope: CompletionScope
    required_contracts: tuple[str, ...]
    declared_contracts: tuple[str, ...]
    satisfied_contracts: tuple[str, ...]
    findings: tuple[CompletionGateFinding, ...]

    @property
    def passed(self) -> bool:
        return not any(item.severity == "blocking" for item in self.findings)

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": COMPLETION_GATE_REPORT_API_VERSION,
            "feature_id": self.feature_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "risk": self.risk.value,
            "scope": self.scope.value,
            "passed": self.passed,
            "required_contracts": list(self.required_contracts),
            "declared_contracts": list(self.declared_contracts),
            "satisfied_contracts": list(self.satisfied_contracts),
            "findings": [item.as_dict() for item in self.findings],
        }

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.body_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + sha256(payload).hexdigest()

    def as_dict(self) -> dict[str, object]:
        result = self.body_dict()
        result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )


__all__ = [
    "COMPLETION_GATE_REPORT_API_VERSION",
    "CompletionGateReport",
]
