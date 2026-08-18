from __future__ import annotations

from typing import Sequence

from sdai.asyncapi_contracts import AsyncAPIContractAdapter
from sdai.contracts import ContractAdapterRegistry, ContractSnapshot
from sdai.json_schema_contracts import JSONSchemaContractAdapter
from sdai.openapi_directional import OpenAPIContractAdapter
from sdai.protobuf_directional import ProtobufContractAdapter


def default_contract_registry(
    sources: Sequence[ContractSnapshot] = (),
) -> ContractAdapterRegistry:
    """Build the deterministic built-in contract adapter registry."""
    return ContractAdapterRegistry(
        [
            AsyncAPIContractAdapter(),
            JSONSchemaContractAdapter(),
            OpenAPIContractAdapter(),
            ProtobufContractAdapter(sources),
        ]
    )
