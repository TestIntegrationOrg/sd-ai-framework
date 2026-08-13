from __future__ import annotations

from pathlib import Path
from typing import Mapping

from sdai.completion_policy import CompletionDimension
from sdai.completion_report import CompletionFinding
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.trace_evidence import EvidenceKind, EvidenceStatus, load_trace_evidence
from sdai.trace_freshness import CommitPolicy, ProofFreshness, evaluate_trace_evidence_file


class CompletionTraceCheckError(RuntimeError):
    pass


_TYPED_KIND: Mapping[CompletionDimension, EvidenceKind] = {
    CompletionDimension.TEST: EvidenceKind.TEST,
    CompletionDimension.QUALITY: EvidenceKind.QUALITY,
    CompletionDimension.SECURITY: EvidenceKind.SECURITY,
    CompletionDimension.APPROVAL: EvidenceKind.APPROVAL,
}


def typed_evidence_finding(
    root: Path,
    dimension: CompletionDimension,
    raw_path: Path | str | None,
    *,
    head: str,
    expected_subjects: set[str] | None,
) -> CompletionFinding:
    if dimension not in _TYPED_KIND:
        raise CompletionTraceCheckError(f"SDAI-COMPLETE-TRACE-001: {dimension.value} is not typed trace evidence")
    if raw_path is None:
        return CompletionFinding(dimension, "missing", f"required {dimension.value} evidence path was not supplied")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        safe = ensure_within_project(root, candidate, label=f"completion {dimension.value} evidence")
    except PathSafetyError as exc:
        raise CompletionTraceCheckError(f"SDAI-COMPLETE-TRACE-002: evidence path escapes project root: {raw_path}") from exc
    source = safe.relative_to(root).as_posix()
    if not safe.exists():
        return CompletionFinding(dimension, "missing", "typed evidence record is missing", source)
    try:
        record = load_trace_evidence(root, safe)
    except Exception as exc:
        return CompletionFinding(dimension, "blocked", f"typed evidence is invalid: {exc}", source)
    expected_kind = _TYPED_KIND[dimension]
    if record.kind is not expected_kind:
        return CompletionFinding(dimension, "wrong-subject", f"expected {expected_kind.value} evidence, found {record.kind.value}", source)
    if expected_subjects is not None and record.subject not in expected_subjects:
        return CompletionFinding(dimension, "wrong-subject", f"typed evidence subject {record.subject!r} does not match completion subject", source)
    if record.status is not EvidenceStatus.PASSED:
        return CompletionFinding(dimension, "failed", f"typed evidence status is {record.status.value}", source)
    try:
        freshness = evaluate_trace_evidence_file(root, safe, commit_policy=CommitPolicy.EXACT_HEAD)
    except Exception as exc:
        return CompletionFinding(dimension, "blocked", f"typed evidence freshness evaluation failed: {exc}", source)
    if freshness.current_git_commit != head or freshness.freshness is not ProofFreshness.VALID:
        return CompletionFinding(dimension, "stale", "typed evidence is not valid for exact current HEAD/content", source)
    return CompletionFinding(dimension, "valid", "typed evidence is passed and exact-HEAD current", source)


__all__ = ["CompletionTraceCheckError", "typed_evidence_finding"]
