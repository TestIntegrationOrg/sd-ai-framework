from __future__ import annotations

import argparse
from pathlib import Path

from sdai.artifact_schemas import load_artifact_schema_graph


def add_schema_parser(commands: argparse._SubParsersAction) -> None:
    schema = commands.add_parser(
        "schema",
        help="Inspect and validate the effective artifact schema graph",
    )
    actions = schema.add_subparsers(dest="schema_action", required=True)

    list_command = actions.add_parser("list", help="List effective artifact definitions")
    list_command.add_argument("--json", action="store_true")
    list_command.add_argument("--path")

    show = actions.add_parser("show", help="Show one effective artifact definition")
    show.add_argument("artifact")
    show.add_argument("--json", action="store_true")
    show.add_argument("--path")

    validate = actions.add_parser("validate", help="Validate the effective artifact DAG")
    validate.add_argument("--json", action="store_true")
    validate.add_argument("--path")

    graph = actions.add_parser("graph", help="Show the effective artifact dependency graph")
    graph.add_argument("--json", action="store_true")
    graph.add_argument("--path")


def run_schema_command(root: Path, args: argparse.Namespace) -> int:
    graph = load_artifact_schema_graph(root)
    if args.schema_action == "list":
        if args.json:
            print(graph.to_json())
        else:
            for artifact in graph.artifacts:
                print(
                    f"{artifact.id} type={artifact.type} required={str(artifact.required).lower()} "
                    f"source={artifact.source_layer.value}:{artifact.source} path={artifact.path}"
                )
        return 0

    if args.schema_action == "show":
        artifact = graph.by_id().get(args.artifact)
        if artifact is None:
            raise ValueError(f"Unknown artifact schema id: {args.artifact}")
        if args.json:
            import json

            print(json.dumps(artifact.as_dict(), indent=2, sort_keys=True, ensure_ascii=False))
        else:
            print(
                f"Artifact {artifact.id}\n"
                f"  path: {artifact.path}\n"
                f"  type: {artifact.type}\n"
                f"  required: {str(artifact.required).lower()}\n"
                f"  locked: {str(artifact.locked).lower()}\n"
                f"  depends_on: {', '.join(artifact.depends_on) or '-'}\n"
                f"  applies_to: {', '.join(artifact.applies_to)}\n"
                f"  source: {artifact.source_layer.value}:{artifact.source}"
            )
            for contribution in artifact.history:
                print(
                    f"  provenance: {contribution.layer.value}:{contribution.source} "
                    f"fields={','.join(contribution.fields) or '-'}"
                )
        return 0

    if args.schema_action == "validate":
        if args.json:
            print(graph.to_json())
        else:
            print(
                f"Validated artifact schema artifacts={len(graph.artifacts)} "
                f"edges={sum(len(item.depends_on) for item in graph.artifacts)} "
                f"sources={len(graph.sources)}"
            )
        return 0

    if args.schema_action == "graph":
        if args.json:
            print(graph.to_json())
        else:
            print("Artifact dependency order:")
            for index, artifact_id in enumerate(graph.topological_order, start=1):
                artifact = graph.by_id()[artifact_id]
                print(
                    f"  {index:02d}. {artifact_id} <- "
                    f"{', '.join(artifact.depends_on) or '<root>'}"
                )
        return 0

    raise ValueError(f"Unknown schema action: {args.schema_action}")
