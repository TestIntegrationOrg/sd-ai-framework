from __future__ import annotations

import argparse
from pathlib import Path

from sdai.multi_repo_feature_graph import FeatureGraphFindingLevel, MultiRepoFeatureGraph
from sdai.multi_repo_pr_graph import build_multi_repo_feature_graph


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdai feature graph",
        description="Build the canonical read-only multi-repository feature graph",
    )
    parser.add_argument("feature")
    parser.add_argument("--json", action="store_true", help="Emit canonical JSON")
    parser.add_argument("--path", help="SD-AI project root")
    return parser


def _human(graph: MultiRepoFeatureGraph) -> None:
    repositories = sum(1 for node in graph.nodes if node.type.value == "repository")
    stores = sum(1 for node in graph.nodes if node.type.value == "store")
    errors = sum(
        1 for finding in graph.findings if finding.level is FeatureGraphFindingLevel.ERROR
    )
    warnings = sum(
        1 for finding in graph.findings if finding.level is FeatureGraphFindingLevel.WARNING
    )
    print(
        f"Feature graph feature={graph.feature_id} nodes={len(graph.nodes)} "
        f"edges={len(graph.edges)} stores={stores} repositories={repositories} "
        f"errors={errors} warnings={warnings} sha256={graph.sha256}"
    )
    for node in graph.nodes:
        participants = ",".join(sorted({fact.participant for fact in node.facts}))
        print(f"  NODE {node.node_id} type={node.type.value} participants={participants}")
    for edge in graph.edges:
        print(f"  EDGE {edge.source} --{edge.relation}--> {edge.target}")
    for finding in graph.findings:
        participant = f" participant={finding.participant}" if finding.participant else ""
        print(
            f"  {finding.level.value.upper():7} {finding.code} "
            f"subject={finding.subject}{participant}: {finding.message}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.path or ".").resolve()
    graph = build_multi_repo_feature_graph(root, args.feature)
    if args.json:
        print(graph.to_json())
    else:
        _human(graph)
    return 2 if graph.has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
