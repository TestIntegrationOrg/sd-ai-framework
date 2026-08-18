from __future__ import annotations

from typing import Mapping, Sequence

from sdai.contracts import (
    CompatibilityDirection,
    ContractFinding,
    ContractSeverity,
    ContractSnapshot,
)
from sdai.protobuf_contracts import (
    ProtoDocument,
    ProtoField,
    ProtoImport,
    ProtoMessage,
    ProtoRpc,
    ProtoService,
    ProtobufContractAdapter as _BaseProtobufContractAdapter,
    _one_way_diff,
    _parse,
    _resolve_import,
)


_SCALARS = frozenset(
    {
        "double",
        "float",
        "int32",
        "int64",
        "uint32",
        "uint64",
        "sint32",
        "sint64",
        "fixed32",
        "fixed64",
        "sfixed32",
        "sfixed64",
        "bool",
        "string",
        "bytes",
    }
)


def _declared_types(document: ProtoDocument) -> frozenset[str]:
    return frozenset(
        [item.full_name for item in document.messages]
        + [item.full_name for item in document.enums]
    )


def _canonical_type(type_name: str, *, owner: str | None, document: ProtoDocument) -> str:
    if type_name.startswith("map<") and type_name.endswith(">"):
        inner = type_name[4:-1]
        if "," not in inner:
            return type_name
        key, value = inner.split(",", 1)
        return (
            "map<"
            + _canonical_type(key, owner=owner, document=document)
            + ","
            + _canonical_type(value, owner=owner, document=document)
            + ">"
        )
    if type_name in _SCALARS or type_name.startswith("."):
        return type_name

    declarations = _declared_types(document)
    parts = type_name.split(".")
    if owner:
        scope = owner.lstrip(".").split(".")
        for size in range(len(scope), -1, -1):
            candidate = "." + ".".join([*scope[:size], *parts])
            if candidate in declarations:
                return candidate

    if "." in type_name:
        return "." + type_name
    if document.package:
        return f".{document.package}.{type_name}"
    return "." + type_name


def _canonical_imports(
    snapshot: ContractSnapshot,
    document: ProtoDocument,
    sources: Mapping[str, ContractSnapshot],
    *,
    importer_path: str,
) -> tuple[ProtoImport, ...]:
    canonical: list[ProtoImport] = []
    for imported in document.imports:
        target, error = _resolve_import(
            snapshot,
            imported,
            sources,
            importer_path=importer_path,
        )
        path = target.source.path if target is not None and error is None else imported.path
        canonical.append(ProtoImport(path=path, modifier=imported.modifier))
    return tuple(sorted(canonical, key=lambda item: (item.path, item.modifier)))


def _canonical_document(
    snapshot: ContractSnapshot,
    document: ProtoDocument,
    sources: Mapping[str, ContractSnapshot],
    *,
    importer_path: str,
) -> ProtoDocument:
    messages = tuple(
        ProtoMessage(
            full_name=message.full_name,
            fields=tuple(
                ProtoField(
                    name=field.name,
                    number=field.number,
                    type_name=_canonical_type(
                        field.type_name,
                        owner=message.full_name,
                        document=document,
                    ),
                    cardinality=field.cardinality,
                )
                for field in message.fields
            ),
            reserved_ranges=message.reserved_ranges,
            reserved_names=message.reserved_names,
        )
        for message in document.messages
    )
    services = tuple(
        ProtoService(
            full_name=service.full_name,
            rpcs=tuple(
                ProtoRpc(
                    name=rpc.name,
                    request_type=_canonical_type(
                        rpc.request_type,
                        owner=None,
                        document=document,
                    ),
                    response_type=_canonical_type(
                        rpc.response_type,
                        owner=None,
                        document=document,
                    ),
                    client_streaming=rpc.client_streaming,
                    server_streaming=rpc.server_streaming,
                )
                for rpc in service.rpcs
            ),
        )
        for service in document.services
    )
    return ProtoDocument(
        syntax=document.syntax,
        package=document.package,
        imports=_canonical_imports(
            snapshot,
            document,
            sources,
            importer_path=importer_path,
        ),
        messages=messages,
        enums=document.enums,
        services=services,
    )


class ProtobufContractAdapter(_BaseProtobufContractAdapter):
    """Directional Protobuf adapter with canonical symbol/import comparison."""

    def diff(
        self,
        before: ContractSnapshot,
        after: ContractSnapshot,
        direction: CompatibilityDirection,
    ) -> Sequence[ContractFinding]:
        sources = self._source_index(before, after)
        logical_path = before.source.path
        baseline = _parse(before, sources, importer_path=logical_path)
        candidate = _parse(after, sources, importer_path=logical_path)
        parse_findings = [*baseline.findings, *candidate.findings]
        if (
            baseline.document is None
            or candidate.document is None
            or any(item.severity is ContractSeverity.ERROR for item in parse_findings)
        ):
            return tuple(parse_findings)

        baseline_document = _canonical_document(
            before,
            baseline.document,
            sources,
            importer_path=logical_path,
        )
        candidate_document = _canonical_document(
            after,
            candidate.document,
            sources,
            importer_path=logical_path,
        )

        if direction is CompatibilityDirection.FORWARD:
            return tuple(
                _one_way_diff(
                    candidate_document,
                    baseline_document,
                    snapshot=before,
                    direction=CompatibilityDirection.FORWARD,
                )
            )
        if direction is CompatibilityDirection.FULL:
            return tuple(
                [
                    *_one_way_diff(
                        baseline_document,
                        candidate_document,
                        snapshot=after,
                        direction=CompatibilityDirection.BACKWARD,
                    ),
                    *_one_way_diff(
                        candidate_document,
                        baseline_document,
                        snapshot=before,
                        direction=CompatibilityDirection.FORWARD,
                    ),
                ]
            )
        return tuple(
            _one_way_diff(
                baseline_document,
                candidate_document,
                snapshot=after,
                direction=CompatibilityDirection.BACKWARD,
            )
        )
