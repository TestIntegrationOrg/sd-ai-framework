from __future__ import annotations

from sdai.asyncapi_contracts import AsyncAPIContractAdapter
from sdai.contracts import ContractAdapterRegistry
from sdai.openapi_directional import OpenAPIContractAdapter


def default_contract_registry() -> ContractAdapterRegistry:
    """Build the deterministic built-in contract adapter registry."""
    return ContractAdapterRegistry([AsyncAPIContractAdapter(), OpenAPIContractAdapter()])
