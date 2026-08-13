from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess

from sdai.trace_builder import build_feature_trace_graph
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance
from sdai.version_entrypoint import main


FEATURE = "TRACE-107"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=True,
        shell=False,
    )
    return result.stdout.strip()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _digest(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def _repo(root: Path, *, second_requirement: bool = False, unresolved: bool = False) -> Path:
    _git(root, "init")
    _git(root, "config", "user.email", "sdai@example.test")
    _git(root, "config", "user.name", "SDAI Test")
    _write(root / ".sdai" / "config.yaml", "version: 1\n")
    feature = root / "specs" / "changes" / FEATURE
    extra = "- FR-002: Additional requirement.\n" if second_requirement else ""
    missing = " References ADR-MISSING." if unresolved else ""
    _write(
        feature / "requirements.md",
        f"""# Requirements

- FR-001: Sign café scripts.{missing}
{extra}- ADR-001: Use deterministic traceability.
""",
    )
    _write(
        root / "src" / "café.py",
        "# Trace: FR-001 ADR-001\nVALUE = 1\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    commit = _git(root, "rev-parse", "HEAD")

    record = TraceEvidence(
        evidence_id="EVIDENCE-001",
        kind=EvidenceKind.TEST,
        status=EvidenceStatus.PASSED,
        subject="requirement:FR-001",
        git_commit=commit,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.SOURCE,
                "src/café.py",
                _digest(root / "src" / "café.py"),
            ),
        ),
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/evidence/test.json",
                1,
            ),
        ),
        producer=EvidenceProducer("tester", "codex", "model-a"),
        result={"passed": 1},
        command=("python", "-m", "pytest"),
        tool="pytest",
    )
    _write(feature / "evidence" / "test.json", record.to_json())
    return feature


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def test_trace_summary_human_output_has_edges_provenance_utf8_and_is_read_only(
    tmp_path: Path,
    capsys,
) -> None:
    _repo(tmp_path, unresolved=True)
    before = _snapshot(tmp_path)

    exit_code = main(["trace", FEATURE, "--path", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Trace feature=TRACE-107" in captured.out
    assert "--references-->" in captured.out
    assert "source=" in captured.out and ":" in captured.out
    assert "café.py" in captured.out
    assert "ADR-MISSING" in captured.out
    assert captured.err == ""
    assert _snapshot(tmp_path) == before


def test_trace_summary_json_is_machine_clean(tmp_path: Path, capsys) -> None:
    _repo(tmp_path)

    exit_code = main(["trace", FEATURE, "--json", "--path", str(tmp_path)])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert payload["apiVersion"] == "sdai.trace-summary/v1"
    assert payload["feature_id"] == FEATURE
    assert payload["nodes"] > 0
    assert payload["graph_sha256"].startswith("sha256:")


def test_trace_requirement_explains_current_proof_and_relationships(tmp_path: Path, capsys) -> None:
    _repo(tmp_path)

    exit_code = main(
        ["trace", "requirement", FEATURE, "FR-001", "--json", "--path", str(tmp_path)]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["requirement"]["node_id"] == "requirement:FR-001"
    assert payload["covered"] is True
    assert any(item["relation"] == "evidenced-by" for item in payload["outgoing"])
    assert payload["proofs"][0]["freshness"] == "valid"
    assert payload["proofs"][0]["satisfies_current_coverage"] is True


def test_trace_requirement_not_found_has_stable_exit_code(tmp_path: Path, capsys) -> None:
    _repo(tmp_path)

    exit_code = main(
        ["trace", "requirement", FEATURE, "FR-999", "--json", "--path", str(tmp_path)]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["found"] is False
    assert payload["requirement_id"] == "FR-999"


def test_trace_missing_reports_unresolved_links_and_uncovered_requirements(tmp_path: Path, capsys) -> None:
    _repo(tmp_path, second_requirement=True, unresolved=True)

    exit_code = main(["trace", "missing", FEATURE, "--json", "--path", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["apiVersion"] == "sdai.trace-missing/v1"
    assert any(item["target"] == "ADR-MISSING" for item in payload["gaps"])
    assert any(
        item["kind"] == "uncovered-requirement" and item["target"] == "FR-002"
        for item in payload["gaps"]
    )


def test_trace_coverage_reports_current_valid_requirement_percentage(tmp_path: Path, capsys) -> None:
    _repo(tmp_path, second_requirement=True)

    exit_code = main(["trace", "coverage", FEATURE, "--json", "--path", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["apiVersion"] == "sdai.trace-coverage/v1"
    assert payload["requirements_total"] == 2
    assert payload["requirements_covered"] == 1
    assert payload["requirements_uncovered"] == 1
    assert payload["coverage_percent"] == 50.0
    assert payload["proof_counts"]["valid"] == 1


def test_trace_coverage_returns_zero_when_all_requirements_have_current_proof(tmp_path: Path, capsys) -> None:
    _repo(tmp_path)

    exit_code = main(["trace", "coverage", FEATURE, "--json", "--path", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["requirements_total"] == 1
    assert payload["requirements_covered"] == 1
    assert payload["coverage_percent"] == 100.0


def test_trace_export_is_exact_canonical_graph_json(tmp_path: Path, capsys) -> None:
    _repo(tmp_path)
    expected = build_feature_trace_graph(tmp_path, FEATURE, environ={}).graph.to_json()

    exit_code = main(
        ["trace", "export", FEATURE, "--format", "json", "--path", str(tmp_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == expected
    assert json.loads(captured.out)["apiVersion"] == "sdai.trace-graph/v1"


def test_trace_help_is_available_at_top_level_dispatch(tmp_path: Path, capsys) -> None:
    exit_code = main(["trace", "--help"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "sdai trace requirement" in captured.out
    assert "sdai trace coverage" in captured.out