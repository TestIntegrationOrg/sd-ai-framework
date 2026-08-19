from __future__ import annotations

from sdai.architecture_trace import build_feature_trace_graph_with_architecture
from sdai import trace_cli


def main(argv: list[str] | None = None) -> int:
    """Run the existing trace CLI with architecture projection on its canonical builder hook."""
    original = trace_cli.build_feature_trace_graph
    trace_cli.build_feature_trace_graph = build_feature_trace_graph_with_architecture
    try:
        return trace_cli.main(argv)
    finally:
        trace_cli.build_feature_trace_graph = original


__all__ = ["main"]
