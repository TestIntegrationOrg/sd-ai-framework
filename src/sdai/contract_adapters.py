from __future__ import annotations

from sdai.contracts import ContractAdapterRegistry
from sdai.openapi_contracts import OpenAPIContractAdapter


def default_contract_registry() -> ContractAdapterRegistry:
    """Build the deterministic built-in contract adapter registry."""
    return ContractAdapterRegistry([OpenAPIContractAdapter()])
