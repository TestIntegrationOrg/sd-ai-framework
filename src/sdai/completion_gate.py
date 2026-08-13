from __future__ import annotations

from dataclasses import dataclass


COMPLETION_GATE_REPORT_API_VERSION = "sdai.completion-gate-report/v1"


class CompletionGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompletionGateFinding:
    code: str
    severity: str
    contract: str | None
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "contract": self.contract,
            "message": self.message,
        }
