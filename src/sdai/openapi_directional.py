from __future__ import annotations

from typing import Sequence

from sdai.contracts import CompatibilityDirection, ContractFinding, ContractSeverity, ContractSnapshot
from sdai.openapi_contracts import OpenAPIContractAdapter as _OpenAPIContractAdapter
from sdai.openapi_contracts import _diff_documents, _parse


class OpenAPIContractAdapter(_OpenAPIContractAdapter):
    """Direction-aware OpenAPI adapter used by the built-in contract registry."""

    def diff(
        self,
        before: ContractSnapshot,
        after: ContractSnapshot,
        direction: CompatibilityDirection,
    ) -> Sequence[ContractFinding]:
        baseline = _parse(before)
        candidate = _parse(after)
        invalid = [*baseline.findings, *candidate.findings]
        if baseline.document is None or candidate.document is None or any(
            item.severity is ContractSeverity.ERROR for item in invalid
        ):
            return tuple(invalid)

        if direction is CompatibilityDirection.FORWARD:
            return tuple(
                _diff_documents(
                    candidate.document,
                    baseline.document,
                    before,
                    CompatibilityDirection.FORWARD,
                )
            )
        if direction is CompatibilityDirection.FULL:
            backward = _diff_documents(
                baseline.document,
                candidate.document,
                after,
                CompatibilityDirection.BACKWARD,
            )
            forward = _diff_documents(
                candidate.document,
                baseline.document,
                before,
                CompatibilityDirection.FORWARD,
            )
            return tuple([*backward, *forward])
        return tuple(
            _diff_documents(
                baseline.document,
                candidate.document,
                after,
                CompatibilityDirection.BACKWARD,
            )
        )
