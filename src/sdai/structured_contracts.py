from __future__ import annotations

from typing import Mapping, Sequence

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver


MAX_STRUCTURED_NODES = 200_000
MAX_STRUCTURED_DEPTH = 64


class UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate mapping key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def escape_json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def normalize_structured_json(
    value: object,
    *,
    pointer: str = "",
    depth: int = 0,
    ancestors: frozenset[int] = frozenset(),
    counter: list[int] | None = None,
    max_nodes: int = MAX_STRUCTURED_NODES,
    max_depth: int = MAX_STRUCTURED_DEPTH,
) -> object:
    """Normalize parsed YAML/JSON into bounded, finite, deterministic JSON data."""
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > max_nodes:
        raise ValueError("document exceeds the maximum value count")
    if depth > max_depth:
        raise ValueError("document exceeds the maximum nesting depth")
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
            raise ValueError(f"{pointer or '/'} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in ancestors:
            raise ValueError(f"{pointer or '/'} contains a recursive YAML alias")
        next_ancestors = ancestors | {identity}
        normalized: dict[str, object] = {}
        for key in sorted(value, key=lambda item: str(item)):
            if not isinstance(key, str):
                raise ValueError(f"{pointer or '/'} contains a non-string mapping key")
            child = f"{pointer}/{escape_json_pointer(key)}" if pointer else f"/{escape_json_pointer(key)}"
            normalized[key] = normalize_structured_json(
                value[key],
                pointer=child,
                depth=depth + 1,
                ancestors=next_ancestors,
                counter=counter,
                max_nodes=max_nodes,
                max_depth=max_depth,
            )
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in ancestors:
            raise ValueError(f"{pointer or '/'} contains a recursive YAML alias")
        next_ancestors = ancestors | {identity}
        return [
            normalize_structured_json(
                item,
                pointer=f"{pointer}/{index}" if pointer else f"/{index}",
                depth=depth + 1,
                ancestors=next_ancestors,
                counter=counter,
                max_nodes=max_nodes,
                max_depth=max_depth,
            )
            for index, item in enumerate(value)
        ]
    raise ValueError(f"{pointer or '/'} contains an unsupported YAML value")


def resolve_local_json_pointer(document: Mapping[str, object], reference: str) -> object:
    """Resolve an RFC-6901-style local fragment reference only."""
    if not reference.startswith("#/"):
        raise ValueError("external references are not allowed")
    current: object = document
    for raw in reference[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise KeyError(reference)
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                raise KeyError(reference)
            current = current[int(token)]
        else:
            raise KeyError(reference)
    return current
