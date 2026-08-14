from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

from sdai.pack_certification import (
    PackCertificationDecision,
    PackCertificationError,
    PackEvalEvidence,
    ResolvedPackCertificationPolicy,
    evaluate_pack_certification,
)
from sdai.pack_manifest import PackManifest


PACK_EVAL_SUITE_API_VERSION = "sdai.pack-eval-suite/v1"


def _fail(code: str, message: str) -> PackCertificationError:
    return PackCertificationError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-PACK-CERT-001", "eval suite is not canonical finite JSON") from exc


def _hash(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise _fail("SDAI-PACK-CERT-001", f"{label} must be a non-empty identifier")
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_."
    if value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789" or any(char not in allowed for char in value):
        raise _fail("SDAI-PACK-CERT-001", f"{label} '{value}' is not portable lowercase syntax")
    if value[-1] in "-_." or any(pair in value for pair in ("..", "--", "__", "-.", ".-", "-_", "_-", "._", "_.")):
        raise _fail("SDAI-PACK-CERT-001", f"{label} '{value}' is not canonical")
    return value


@dataclass(frozen=True)
class PackEvalCaseSpec:
    id: str
    dimension: str
    case_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.id, label="eval case id")
        _identifier(self.dimension, label="eval dimension")
        if not _valid_hash(self.case_sha256):
            raise _fail("SDAI-PACK-CERT-001", "caseSha256 must be SHA-256")

    def as_dict(self) -> dict[str, str]:
        return {"caseSha256": self.case_sha256, "dimension": self.dimension, "id": self.id}

    @classmethod
    def from_dict(cls, value: object) -> "PackEvalCaseSpec":
        if not isinstance(value, Mapping) or set(value) != {"caseSha256", "dimension", "id"}:
            raise _fail("SDAI-PACK-CERT-001", "eval suite case contract is invalid")
        return cls(
            id=_identifier(value["id"], label="eval case id"),
            dimension=_identifier(value["dimension"], label="eval dimension"),
            case_sha256=value["caseSha256"] if _valid_hash(value["caseSha256"]) else "",
        )


@dataclass(frozen=True)
class PackEvalSuite:
    cases: tuple[PackEvalCaseSpec, ...]

    def __post_init__(self) -> None:
        if not self.cases:
            raise _fail("SDAI-PACK-CERT-001", "eval suite must contain at least one case")
        if self.cases != tuple(sorted(self.cases, key=lambda item: item.id)):
            raise _fail("SDAI-PACK-CERT-001", "eval suite cases must be sorted by id")
        if len({item.id for item in self.cases}) != len(self.cases):
            raise _fail("SDAI-PACK-CERT-001", "eval suite case ids must be unique")

    def as_dict(self) -> dict[str, object]:
        return {"apiVersion": PACK_EVAL_SUITE_API_VERSION, "cases": [item.as_dict() for item in self.cases]}

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _hash(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> "PackEvalSuite":
        if not isinstance(value, Mapping) or set(value) != {"apiVersion", "cases"}:
            raise _fail("SDAI-PACK-CERT-001", "Pack eval suite contract is invalid")
        if value["apiVersion"] != PACK_EVAL_SUITE_API_VERSION:
            raise _fail("SDAI-PACK-CERT-001", f"Pack eval suite apiVersion must be '{PACK_EVAL_SUITE_API_VERSION}'")
        cases = value["cases"]
        if not isinstance(cases, list):
            raise _fail("SDAI-PACK-CERT-001", "Pack eval suite cases must be a list")
        parsed = tuple(sorted((PackEvalCaseSpec.from_dict(item) for item in cases), key=lambda item: item.id))
        return cls(parsed)

    @classmethod
    def from_json(cls, text: str) -> "PackEvalSuite":
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-CERT-001", "Pack eval suite JSON is malformed") from exc


def load_pack_eval_suite(path: Path) -> PackEvalSuite:
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-PACK-CERT-003", "Pack eval suite must be a regular non-symlink file")
    try:
        return PackEvalSuite.from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-PACK-CERT-003", "unable to read Pack eval suite as UTF-8") from exc


def evaluate_pack_certification_suite(
    manifest: PackManifest,
    content_sha256: str,
    policy: ResolvedPackCertificationPolicy,
    suite: PackEvalSuite,
    evidence: PackEvalEvidence | None,
    *,
    risk: str = "standard",
) -> PackCertificationDecision:
    if evidence is not None and evidence.suite_sha256 != suite.sha256:
        return PackCertificationDecision(
            status="stale",
            pack_identity=manifest.identity,
            policy_sha256=policy.sha256,
            evidence_truth_sha256=evidence.truth_sha256,
            aggregate_score_basis_points=None,
            dimension_scores=(),
            reasons=("eval-suite-changed",),
            producer=evidence.producer,
        )

    if evidence is not None:
        expected = {item.id: item for item in suite.cases}
        actual = {item.id: item for item in evidence.cases}
        reasons: list[str] = []
        if set(expected) != set(actual):
            reasons.append("eval-case-set-mismatch")
        for case_id in sorted(set(expected) & set(actual)):
            spec = expected[case_id]
            result = actual[case_id]
            if spec.dimension != result.dimension or spec.case_sha256 != result.case_sha256:
                reasons.append(f"eval-case-input-mismatch:{case_id}")
        if reasons:
            return PackCertificationDecision(
                status="failed",
                pack_identity=manifest.identity,
                policy_sha256=policy.sha256,
                evidence_truth_sha256=evidence.truth_sha256,
                aggregate_score_basis_points=None,
                dimension_scores=(),
                reasons=tuple(sorted(reasons)),
                producer=evidence.producer,
            )

    return evaluate_pack_certification(
        manifest,
        content_sha256,
        policy,
        evidence,
        risk=risk,
    )
