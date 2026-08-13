from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

from sdai.trace_builder import TraceBuildResult, TraceGap, build_feature_trace_graph
from sdai.trace_freshness import (
    EvidenceFreshnessReport,
    ProofFreshness,
    evaluate_trace_coverage,
    evaluate_trace_evidence_file,
)
from sdai.trace_graph import TraceEdge, TraceNode, TraceNodeType


_TRACE_ACTIONS = frozenset({"requirement", "missing", "coverage", "export"})
_PROOF_ORDER = {
    ProofFreshness.VALID: 0,
    ProofFreshness.STALE: 1,
    ProofFreshness.BLOCKED: 2,
    ProofFreshness.MISSING: 3,
}


def _root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def _ensure_initialized(root: Path) -> None:
    if not (root / ".sdai" / "config.yaml").exists():
        raise RuntimeError("Not an SD-AI project. Run `sdai init` first.")


def _json(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _provenance_text(node_or_edge: TraceNode | TraceEdge) -> str:
    return ", ".join(
        f"{item.source}:{item.line}"
        for item in node_or_edge.provenance
    ) or "-"


def _summary_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdai trace")
    parser.add_argument("feature")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path")
    return parser


def _requirement_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdai trace requirement")
    parser.add_argument("feature")
    parser.add_argument("requirement_id")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path")
    return parser


def _missing_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdai trace missing")
    parser.add_argument("feature")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path")
    return parser


def _coverage_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdai trace coverage")
    parser.add_argument("feature")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path")
    return parser


def _export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdai trace export")
    parser.add_argument("feature")
    parser.add_argument("--format", choices=["json"], default="json")
    parser.add_argument("--path")
    return parser


def _help() -> None:
    print(
        "Canonical traceability commands:\n"
        "  sdai trace FEATURE [--json] [--path PATH]\n"
        "  sdai trace requirement FEATURE <REQ-ID> [--json] [--path PATH]\n"
        "  sdai trace missing FEATURE [--json] [--path PATH]\n"
        "  sdai trace coverage FEATURE [--json] [--path PATH]\n"
        "  sdai trace export FEATURE --format json [--path PATH]"
    )


def _build(root: Path, feature: str) -> TraceBuildResult:
    _ensure_initialized(root)
    return build_feature_trace_graph(root, feature, environ={})


def _evidence_reports(
    root: Path,
    result: TraceBuildResult,
) -> dict[str, EvidenceFreshnessReport]:
    reports: dict[str, EvidenceFreshnessReport] = {}
    for node in result.graph.nodes:
        if node.type is not TraceNodeType.EVIDENCE:
            continue
        candidates: list[EvidenceFreshnessReport] = []
        seen_sources: set[str] = set()
        for provenance in node.provenance:
            source = provenance.source
            if source in seen_sources:
                continue
            seen_sources.add(source)
            if not source.endswith(".json"):
                continue
            report = evaluate_trace_evidence_file(root, Path(source))
            if report.evidence_id == node.entity_id:
                candidates.append(report)
        if not candidates:
            continue
        reports[node.entity_id] = max(
            candidates,
            key=lambda item: _PROOF_ORDER[item.freshness],
        )
    return reports


def _requirement_rows(
    result: TraceBuildResult,
    reports: Mapping[str, EvidenceFreshnessReport],
) -> list[dict[str, object]]:
    proofs = evaluate_trace_coverage(result.graph, reports)
    by_requirement: dict[str, list[object]] = {}
    for proof in proofs:
        by_requirement.setdefault(proof.source_node_id, []).append(proof)

    rows: list[dict[str, object]] = []
    for node in result.graph.nodes:
        if node.type is not TraceNodeType.REQUIREMENT:
            continue
        node_proofs = by_requirement.get(node.node_id, [])
        valid = [item for item in node_proofs if item.satisfies_current_coverage]
        rows.append(
            {
                "node_id": node.node_id,
                "requirement_id": node.entity_id,
                "covered": bool(valid),
                "proofs": [item.as_dict() for item in node_proofs],
                "provenance": [item.as_dict() for item in node.provenance],
            }
        )
    return rows


def _coverage_payload(
    result: TraceBuildResult,
    reports: Mapping[str, EvidenceFreshnessReport],
) -> dict[str, object]:
    rows = _requirement_rows(result, reports)
    total = len(rows)
    covered = sum(1 for row in rows if row["covered"])
    percent = 100.0 if total == 0 else round((covered * 100.0) / total, 2)
    proof_counts = {
        state.value: sum(
            1
            for report in reports.values()
            if report.freshness is state
        )
        for state in ProofFreshness
    }
    return {
        "apiVersion": "sdai.trace-coverage/v1",
        "feature_id": result.graph.feature_id,
        "graph_sha256": result.graph.sha256,
        "requirements_total": total,
        "requirements_covered": covered,
        "requirements_uncovered": total - covered,
        "coverage_percent": percent,
        "gaps": len(result.gaps),
        "proof_counts": proof_counts,
        "requirements": rows,
    }


def _summary_payload(result: TraceBuildResult) -> dict[str, object]:
    node_counts = {
        node_type.value: sum(1 for node in result.graph.nodes if node.type is node_type)
        for node_type in TraceNodeType
        if any(node.type is node_type for node in result.graph.nodes)
    }
    return {
        "apiVersion": "sdai.trace-summary/v1",
        "feature_id": result.graph.feature_id,
        "graph_sha256": result.graph.sha256,
        "nodes": len(result.graph.nodes),
        "edges": len(result.graph.edges),
        "gaps": len(result.gaps),
        "node_counts": node_counts,
    }


def _print_summary(result: TraceBuildResult) -> None:
    payload = _summary_payload(result)
    print(
        f"Trace feature={result.graph.feature_id} nodes={payload['nodes']} "
        f"edges={payload['edges']} gaps={payload['gaps']} sha256={result.graph.sha256}"
    )
    for node in result.graph.nodes:
        print(
            f"  NODE {node.node_id}"
            f" source={_provenance_text(node)}"
        )
    for edge in result.graph.edges:
        print(
            f"  EDGE {edge.source} --{edge.relation.value}--> {edge.target}"
            f" source={_provenance_text(edge)}"
        )
    for gap in result.gaps:
        print(
            f"  GAP  {gap.kind} target={gap.target} relation={gap.relation} "
            f"source={gap.source}:{gap.line}"
        )


def _requirement_payload(
    result: TraceBuildResult,
    requirement_id: str,
    reports: Mapping[str, EvidenceFreshnessReport],
) -> dict[str, object] | None:
    expected = requirement_id.strip().upper()
    node = next(
        (
            item
            for item in result.graph.nodes
            if item.type is TraceNodeType.REQUIREMENT and item.entity_id.upper() == expected
        ),
        None,
    )
    if node is None:
        return None

    incoming = [edge.as_dict() for edge in result.graph.edges if edge.target == node.node_id]
    outgoing = [edge.as_dict() for edge in result.graph.edges if edge.source == node.node_id]
    proofs = [
        item.as_dict()
        for item in evaluate_trace_coverage(result.graph, reports)
        if item.source_node_id == node.node_id
    ]
    gaps = [
        gap.as_dict()
        for gap in result.gaps
        if gap.source_node_id == node.node_id or gap.target.upper() == expected
    ]
    return {
        "apiVersion": "sdai.trace-requirement/v1",
        "feature_id": result.graph.feature_id,
        "graph_sha256": result.graph.sha256,
        "requirement": node.as_dict(),
        "incoming": incoming,
        "outgoing": outgoing,
        "proofs": proofs,
        "gaps": gaps,
        "covered": any(bool(item["satisfies_current_coverage"]) for item in proofs),
    }


def _uncovered_requirement_gaps(
    result: TraceBuildResult,
    reports: Mapping[str, EvidenceFreshnessReport],
) -> list[dict[str, object]]:
    rows = _requirement_rows(result, reports)
    return [
        {
            "kind": "uncovered-requirement",
            "source": row["provenance"][0]["source"] if row["provenance"] else "-",
            "line": row["provenance"][0]["line"] if row["provenance"] else 0,
            "source_node_id": row["node_id"],
            "target": row["requirement_id"],
            "relation": "evidenced-by",
            "detail": "requirement has no valid current evidence proof",
        }
        for row in rows
        if not row["covered"]
    ]


def _missing_payload(
    result: TraceBuildResult,
    reports: Mapping[str, EvidenceFreshnessReport],
) -> dict[str, object]:
    gaps = [item.as_dict() for item in result.gaps]
    gaps.extend(_uncovered_requirement_gaps(result, reports))
    gaps.sort(
        key=lambda item: (
            str(item["kind"]),
            str(item["source"]).casefold(),
            str(item["source"]),
            int(item["line"]),
            str(item["target"]),
        )
    )
    return {
        "apiVersion": "sdai.trace-missing/v1",
        "feature_id": result.graph.feature_id,
        "graph_sha256": result.graph.sha256,
        "count": len(gaps),
        "gaps": gaps,
    }


def _run_summary(args: argparse.Namespace) -> int:
    root = _root(args.path)
    result = _build(root, args.feature)
    if args.json:
        print(_json(_summary_payload(result)))
    else:
        _print_summary(result)
    return 0


def _run_requirement(args: argparse.Namespace) -> int:
    root = _root(args.path)
    result = _build(root, args.feature)
    reports = _evidence_reports(root, result)
    payload = _requirement_payload(result, args.requirement_id, reports)
    if payload is None:
        if args.json:
            print(
                _json(
                    {
                        "apiVersion": "sdai.trace-requirement/v1",
                        "feature_id": result.graph.feature_id,
                        "requirement_id": args.requirement_id,
                        "found": False,
                    }
                )
            )
        else:
            print(f"Requirement not found: {args.requirement_id}")
        return 2
    if args.json:
        print(_json(payload))
    else:
        requirement = payload["requirement"]
        assert isinstance(requirement, dict)
        print(
            f"Requirement {requirement['node_id']} covered={str(payload['covered']).lower()} "
            f"source={requirement['provenance'][0]['source']}:{requirement['provenance'][0]['line']}"
        )
        for edge in payload["incoming"]:
            print(f"  IN   {edge['source']} --{edge['relation']}--> {edge['target']}")
        for edge in payload["outgoing"]:
            print(f"  OUT  {edge['source']} --{edge['relation']}--> {edge['target']}")
        for proof in payload["proofs"]:
            print(
                f"  PROOF {proof['evidence_id']} freshness={proof['freshness']} "
                f"current={str(proof['satisfies_current_coverage']).lower()}"
            )
        for gap in payload["gaps"]:
            print(f"  GAP  {gap['kind']} target={gap['target']} source={gap['source']}:{gap['line']}")
    return 0


def _run_missing(args: argparse.Namespace) -> int:
    root = _root(args.path)
    result = _build(root, args.feature)
    reports = _evidence_reports(root, result)
    payload = _missing_payload(result, reports)
    if args.json:
        print(_json(payload))
    else:
        print(f"Trace missing feature={result.graph.feature_id} count={payload['count']}")
        for gap in payload["gaps"]:
            print(
                f"  {gap['kind']} target={gap['target']} relation={gap['relation']} "
                f"source={gap['source']}:{gap['line']}"
            )
    return 2 if payload["count"] else 0


def _run_coverage(args: argparse.Namespace) -> int:
    root = _root(args.path)
    result = _build(root, args.feature)
    reports = _evidence_reports(root, result)
    payload = _coverage_payload(result, reports)
    if args.json:
        print(_json(payload))
    else:
        print(
            f"Trace coverage feature={result.graph.feature_id} "
            f"covered={payload['requirements_covered']}/{payload['requirements_total']} "
            f"percent={payload['coverage_percent']:.2f}% gaps={payload['gaps']}"
        )
        for row in payload["requirements"]:
            print(
                f"  {'COVERED' if row['covered'] else 'MISSING':7} "
                f"{row['requirement_id']} proofs={len(row['proofs'])}"
            )
    return 0 if payload["requirements_uncovered"] == 0 else 2


def _run_export(args: argparse.Namespace) -> int:
    root = _root(args.path)
    result = _build(root, args.feature)
    print(result.graph.to_json())
    return 0


def main(argv: list[str] | None = None) -> int:
    effective = list(argv or [])
    if not effective or effective[0] in {"-h", "--help"}:
        _help()
        return 0
    try:
        if effective[0] == "requirement":
            return _run_requirement(_requirement_parser().parse_args(effective[1:]))
        if effective[0] == "missing":
            return _run_missing(_missing_parser().parse_args(effective[1:]))
        if effective[0] == "coverage":
            return _run_coverage(_coverage_parser().parse_args(effective[1:]))
        if effective[0] == "export":
            return _run_export(_export_parser().parse_args(effective[1:]))
        if effective[0] in _TRACE_ACTIONS:
            raise ValueError(f"unsupported trace action: {effective[0]}")
        return _run_summary(_summary_parser().parse_args(effective))
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 1
