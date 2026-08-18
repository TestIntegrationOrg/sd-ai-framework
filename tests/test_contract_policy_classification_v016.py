from __future__ import annotations

from sdai.contract_policy import ContractChangeClass, classify_contract_finding
from sdai.contracts import (
    CompatibilityDirection,
    ContractFinding,
    ContractSeverity,
)


def test_all_four_contract_formats_share_stable_breaking_classification() -> None:
    cases = (
        "SDAI-CONTRACT-OPENAPI-DIFF-001",
        "SDAI-CONTRACT-ASYNCAPI-DIFF-001",
        "SDAI-CONTRACT-JSONSCHEMA-DIFF-010",
        "SDAI-CONTRACT-PROTOBUF-DIFF-001",
    )
    for code in cases:
        classified = classify_contract_finding(
            ContractFinding(
                code=code,
                severity=ContractSeverity.ERROR,
                message="breaking test",
                compatibility=CompatibilityDirection.BACKWARD,
            )
        )
        assert classified.change_class is ContractChangeClass.BREAKING
        assert classified.severity == "error"
        assert classified.compatibility == "backward"


def test_unknown_future_breaking_code_cannot_inherit_allowed_severity() -> None:
    classified = classify_contract_finding(
        ContractFinding(
            code="SDAI-CONTRACT-OPENAPI-DIFF-999",
            severity=ContractSeverity.ERROR,
            message="future semantics",
            compatibility=CompatibilityDirection.FULL,
        )
    )
    assert classified.change_class is ContractChangeClass.UNKNOWN
    assert classified.compatibility == "full"


def test_non_error_validation_information_remains_non_breaking() -> None:
    classified = classify_contract_finding(
        ContractFinding(
            code="SDAI-CONTRACT-JSONSCHEMA-000",
            severity=ContractSeverity.INFO,
            message="effective dialect",
        )
    )
    assert classified.change_class is ContractChangeClass.NON_BREAKING
    assert classified.severity == "info"
