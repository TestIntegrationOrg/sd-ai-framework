from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

from sdai.pack_manifest import PackManifest


PACK_CERTIFICATION_POLICY_API_VERSION = "sdai.pack-certification-policy/v1"
PACK_CERTIFICATION_POLICY_RESOLVED_API_VERSION = "sdai.pack-certification-policy-resolved/v1"
PACK_EVAL_EVIDENCE_API_VERSION = "sdai.pack-eval-evidence/v1"
PACK_CERTIFICATION_DECISION_API_VERSION = "sdai.pack-certification-decision/v1"
_HASH_PREFIX = "sha256:"


class PackCertificationError(RuntimeError):
    pass


def _fail(code: str, message: str) -> PackCertificationError:
    return PackCertificationError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-PACK-CERT-001", "value is not canonical finite JSON") from exc


def _hash(value: object) -> str:
    return _HASH_PREFIX + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(_HASH_PREFIX)
        and len(value) == 71
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise _fail("SDAI-PACK-CERT-001", f"{label} must be a non-empty portable identifier")
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_."
    if value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789" or any(char not in allowed for char in value):
        raise _fail("SDAI-PACK-CERT-001", f"{label} '{value}' is not a portable lowercase identifier")
    if value[-1] in "-_." or any(pair in value for pair in ("..", "--", "__", "-.", ".-", "-_", "_-", "._", "_.")):
        raise _fail("SDAI-PACK-CERT-001", f"{label} '{value}' is not canonical")
    return value


def _score(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
        raise _fail("SDAI-PACK-CERT-001", f"{label} must be an integer from 0 to 10000 basis points")
    return value


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-PACK-CERT-001", f"{label} must be a non-empty string")
    return value.strip()


def _exact_keys(value: Mapping[str, object], expected: set[str], *, label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise _fail("SDAI-PACK-CERT-001", f"{label} contains unsupported field(s): {', '.join(unknown)}")
    if missing:
        raise _fail("SDAI-PACK-CERT-001", f"{label} is missing field(s): {', '.join(missing)}")


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-PACK-CERT-001", f"{label} must be a string-keyed mapping")
    return value


def _identifier_list(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _fail("SDAI-PACK-CERT-001", f"{label} must be a list")
    parsed = tuple(sorted(_identifier(item, label=label) for item in value))
    if len(set(parsed)) != len(parsed):
        raise _fail("SDAI-PACK-CERT-001", f"{label} must not contain duplicates")
    return parsed


def _dimension_thresholds(value: object, *, label: str) -> tuple[tuple[str, int], ...]:
    raw = _mapping(value, label=label)
    parsed = tuple(sorted((_identifier(key, label=f"{label} key"), _score(score, label=f"{label}.{key}")) for key, score in raw.items()))
    return parsed


@dataclass(frozen=True)
class CertificationRequirement:
    minimum_score_basis_points: int = 0
    required_dimensions: tuple[tuple[str, int], ...] = ()
    required_cases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _score(self.minimum_score_basis_points, label="minimumScoreBasisPoints")
        if self.required_dimensions != tuple(sorted(self.required_dimensions)):
            raise _fail("SDAI-PACK-CERT-001", "requiredDimensions must be canonical")
        seen_dimensions: set[str] = set()
        for name, score in self.required_dimensions:
            _identifier(name, label="required dimension")
            _score(score, label=f"requiredDimensions.{name}")
            if name in seen_dimensions:
                raise _fail("SDAI-PACK-CERT-001", "requiredDimensions must be unique")
            seen_dimensions.add(name)
        if self.required_cases != tuple(sorted(set(self.required_cases))):
            raise _fail("SDAI-PACK-CERT-001", "requiredCases must be unique and sorted")
        for case in self.required_cases:
            _identifier(case, label="required case")

    def as_dict(self) -> dict[str, object]:
        return {
            "minimumScoreBasisPoints": self.minimum_score_basis_points,
            "requiredCases": list(self.required_cases),
            "requiredDimensions": {name: score for name, score in self.required_dimensions},
        }

    @classmethod
    def from_dict(cls, value: object, *, label: str = "requirement") -> "CertificationRequirement":
        raw = _mapping(value, label=label)
        _exact_keys(raw, {"minimumScoreBasisPoints", "requiredCases", "requiredDimensions"}, label=label)
        return cls(
            minimum_score_basis_points=_score(raw["minimumScoreBasisPoints"], label=f"{label}.minimumScoreBasisPoints"),
            required_dimensions=_dimension_thresholds(raw["requiredDimensions"], label=f"{label}.requiredDimensions"),
            required_cases=_identifier_list(raw["requiredCases"], label=f"{label}.requiredCases"),
        )


def _requirement_map(value: object, *, label: str) -> tuple[tuple[str, CertificationRequirement], ...]:
    raw = _mapping(value, label=label)
    parsed = tuple(sorted((_identifier(key, label=f"{label} key"), CertificationRequirement.from_dict(item, label=f"{label}.{key}")) for key, item in raw.items()))
    return parsed


@dataclass(frozen=True)
class PackCertificationPolicy:
    require_certification: bool
    default: CertificationRequirement
    capabilities: tuple[tuple[str, CertificationRequirement], ...] = ()
    risks: tuple[tuple[str, CertificationRequirement], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.require_certification, bool):
            raise _fail("SDAI-PACK-CERT-001", "requireCertification must be a boolean")
        for label, values in (("capabilities", self.capabilities), ("risks", self.risks)):
            if values != tuple(sorted(values, key=lambda item: item[0])) or len({key for key, _ in values}) != len(values):
                raise _fail("SDAI-PACK-CERT-001", f"{label} must be unique and sorted")
            for key, _ in values:
                _identifier(key, label=f"{label} key")

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_CERTIFICATION_POLICY_API_VERSION,
            "capabilities": {key: requirement.as_dict() for key, requirement in self.capabilities},
            "default": self.default.as_dict(),
            "requireCertification": self.require_certification,
            "risks": {key: requirement.as_dict() for key, requirement in self.risks},
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _hash(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> "PackCertificationPolicy":
        raw = _mapping(value, label="Pack certification policy")
        _exact_keys(raw, {"apiVersion", "capabilities", "default", "requireCertification", "risks"}, label="Pack certification policy")
        if raw["apiVersion"] != PACK_CERTIFICATION_POLICY_API_VERSION:
            raise _fail("SDAI-PACK-CERT-001", f"Pack certification policy apiVersion must be '{PACK_CERTIFICATION_POLICY_API_VERSION}'")
        require = raw["requireCertification"]
        if not isinstance(require, bool):
            raise _fail("SDAI-PACK-CERT-001", "requireCertification must be a boolean")
        return cls(
            require_certification=require,
            default=CertificationRequirement.from_dict(raw["default"], label="default"),
            capabilities=_requirement_map(raw["capabilities"], label="capabilities"),
            risks=_requirement_map(raw["risks"], label="risks"),
        )

    @classmethod
    def from_json(cls, text: str) -> "PackCertificationPolicy":
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-CERT-001", "Pack certification policy JSON is malformed") from exc


def _merge_requirement(left: CertificationRequirement, right: CertificationRequirement) -> CertificationRequirement:
    dimensions: dict[str, int] = dict(left.required_dimensions)
    for name, threshold in right.required_dimensions:
        dimensions[name] = max(dimensions.get(name, 0), threshold)
    return CertificationRequirement(
        minimum_score_basis_points=max(left.minimum_score_basis_points, right.minimum_score_basis_points),
        required_dimensions=tuple(sorted(dimensions.items())),
        required_cases=tuple(sorted(set(left.required_cases) | set(right.required_cases))),
    )


def _merge_requirement_maps(
    left: tuple[tuple[str, CertificationRequirement], ...],
    right: tuple[tuple[str, CertificationRequirement], ...],
) -> tuple[tuple[str, CertificationRequirement], ...]:
    result = dict(left)
    for key, requirement in right:
        result[key] = _merge_requirement(result.get(key, CertificationRequirement()), requirement)
    return tuple(sorted(result.items()))


@dataclass(frozen=True)
class ResolvedPackCertificationPolicy:
    require_certification: bool
    default: CertificationRequirement
    capabilities: tuple[tuple[str, CertificationRequirement], ...]
    risks: tuple[tuple[str, CertificationRequirement], ...]
    provenance: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_CERTIFICATION_POLICY_RESOLVED_API_VERSION,
            "capabilities": {key: requirement.as_dict() for key, requirement in self.capabilities},
            "default": self.default.as_dict(),
            "provenance": [{"policySha256": digest, "scope": scope} for scope, digest in self.provenance],
            "requireCertification": self.require_certification,
            "risks": {key: requirement.as_dict() for key, requirement in self.risks},
        }

    @property
    def sha256(self) -> str:
        return _hash(self.as_dict())


def resolve_pack_certification_policy(
    *,
    organization: PackCertificationPolicy | None = None,
    repository: PackCertificationPolicy | None = None,
    user: PackCertificationPolicy | None = None,
) -> ResolvedPackCertificationPolicy:
    require = False
    default = CertificationRequirement()
    capabilities: tuple[tuple[str, CertificationRequirement], ...] = ()
    risks: tuple[tuple[str, CertificationRequirement], ...] = ()
    provenance: list[tuple[str, str]] = []
    for scope, policy in (("organization", organization), ("repository", repository), ("user", user)):
        if policy is None:
            continue
        require = require or policy.require_certification
        default = _merge_requirement(default, policy.default)
        capabilities = _merge_requirement_maps(capabilities, policy.capabilities)
        risks = _merge_requirement_maps(risks, policy.risks)
        provenance.append((scope, policy.sha256))
    return ResolvedPackCertificationPolicy(require, default, capabilities, risks, tuple(provenance))


def effective_requirement(policy: ResolvedPackCertificationPolicy, manifest: PackManifest, *, risk: str) -> CertificationRequirement:
    requirement = policy.default
    capability_map = dict(policy.capabilities)
    for capability in sorted(manifest.capabilities):
        configured = capability_map.get(capability)
        if configured is not None:
            requirement = _merge_requirement(requirement, configured)
    risk_requirement = dict(policy.risks).get(_identifier(risk, label="risk"))
    if risk_requirement is not None:
        requirement = _merge_requirement(requirement, risk_requirement)
    return requirement


@dataclass(frozen=True)
class PackEvalCaseResult:
    id: str
    dimension: str
    case_sha256: str
    score_basis_points: int
    passed: bool

    def __post_init__(self) -> None:
        _identifier(self.id, label="eval case id")
        _identifier(self.dimension, label="eval dimension")
        if not _valid_hash(self.case_sha256):
            raise _fail("SDAI-PACK-CERT-001", "caseSha256 must be SHA-256")
        _score(self.score_basis_points, label="scoreBasisPoints")
        if not isinstance(self.passed, bool):
            raise _fail("SDAI-PACK-CERT-001", "eval case passed must be a boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "caseSha256": self.case_sha256,
            "dimension": self.dimension,
            "id": self.id,
            "passed": self.passed,
            "scoreBasisPoints": self.score_basis_points,
        }

    @classmethod
    def from_dict(cls, value: object) -> "PackEvalCaseResult":
        raw = _mapping(value, label="eval case")
        _exact_keys(raw, {"caseSha256", "dimension", "id", "passed", "scoreBasisPoints"}, label="eval case")
        passed = raw["passed"]
        if not isinstance(passed, bool):
            raise _fail("SDAI-PACK-CERT-001", "eval case passed must be a boolean")
        return cls(
            id=_identifier(raw["id"], label="eval case id"),
            dimension=_identifier(raw["dimension"], label="eval dimension"),
            case_sha256=raw["caseSha256"] if _valid_hash(raw["caseSha256"]) else "",
            score_basis_points=_score(raw["scoreBasisPoints"], label="scoreBasisPoints"),
            passed=passed,
        )


@dataclass(frozen=True)
class ProducerMetadata:
    provider: str
    model: str
    runner: str

    def as_dict(self) -> dict[str, str]:
        return {"model": self.model, "provider": self.provider, "runner": self.runner}

    @classmethod
    def from_dict(cls, value: object) -> "ProducerMetadata":
        raw = _mapping(value, label="producer")
        _exact_keys(raw, {"model", "provider", "runner"}, label="producer")
        return cls(
            provider=_string(raw["provider"], label="producer.provider"),
            model=_string(raw["model"], label="producer.model"),
            runner=_string(raw["runner"], label="producer.runner"),
        )


@dataclass(frozen=True)
class PackEvalEvidence:
    pack_identity: str
    manifest_sha256: str
    content_sha256: str
    policy_sha256: str
    suite_sha256: str
    cases: tuple[PackEvalCaseResult, ...]
    producer: ProducerMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.pack_identity, str) or "@" not in self.pack_identity:
            raise _fail("SDAI-PACK-CERT-001", "packIdentity must be an exact publisher/id@version")
        for value, label in ((self.manifest_sha256, "manifestSha256"), (self.content_sha256, "contentSha256"), (self.policy_sha256, "policySha256"), (self.suite_sha256, "suiteSha256")):
            if not _valid_hash(value):
                raise _fail("SDAI-PACK-CERT-001", f"{label} must be SHA-256")
        if not self.cases:
            raise _fail("SDAI-PACK-CERT-001", "certification evidence must contain at least one eval case")
        if self.cases != tuple(sorted(self.cases, key=lambda item: item.id)) or len({item.id for item in self.cases}) != len(self.cases):
            raise _fail("SDAI-PACK-CERT-001", "eval cases must be unique and sorted by id")

    def truth_dict(self) -> dict[str, object]:
        """Provider-independent certification facts; producer metadata is deliberately excluded."""
        return {
            "cases": [item.as_dict() for item in self.cases],
            "contentSha256": self.content_sha256,
            "manifestSha256": self.manifest_sha256,
            "packIdentity": self.pack_identity,
            "policySha256": self.policy_sha256,
            "suiteSha256": self.suite_sha256,
        }

    @property
    def truth_sha256(self) -> str:
        return _hash(self.truth_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_EVAL_EVIDENCE_API_VERSION,
            **self.truth_dict(),
            "producer": self.producer.as_dict(),
            "truthSha256": self.truth_sha256,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> "PackEvalEvidence":
        raw = _mapping(value, label="Pack eval evidence")
        expected = {"apiVersion", "cases", "contentSha256", "manifestSha256", "packIdentity", "policySha256", "producer", "suiteSha256", "truthSha256"}
        _exact_keys(raw, expected, label="Pack eval evidence")
        if raw["apiVersion"] != PACK_EVAL_EVIDENCE_API_VERSION:
            raise _fail("SDAI-PACK-CERT-001", f"Pack eval evidence apiVersion must be '{PACK_EVAL_EVIDENCE_API_VERSION}'")
        cases = raw["cases"]
        if not isinstance(cases, list):
            raise _fail("SDAI-PACK-CERT-001", "cases must be a list")
        evidence = cls(
            pack_identity=_string(raw["packIdentity"], label="packIdentity"),
            manifest_sha256=raw["manifestSha256"] if _valid_hash(raw["manifestSha256"]) else "",
            content_sha256=raw["contentSha256"] if _valid_hash(raw["contentSha256"]) else "",
            policy_sha256=raw["policySha256"] if _valid_hash(raw["policySha256"]) else "",
            suite_sha256=raw["suiteSha256"] if _valid_hash(raw["suiteSha256"]) else "",
            cases=tuple(PackEvalCaseResult.from_dict(item) for item in cases),
            producer=ProducerMetadata.from_dict(raw["producer"]),
        )
        if raw["truthSha256"] != evidence.truth_sha256:
            raise _fail("SDAI-PACK-CERT-002", "truthSha256 does not match provider-independent evidence facts")
        return evidence

    @classmethod
    def from_json(cls, text: str) -> "PackEvalEvidence":
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-CERT-001", "Pack eval evidence JSON is malformed") from exc


def _threshold_satisfied(results: tuple[PackEvalCaseResult, ...], threshold: int) -> bool:
    # Compare exact integer sums instead of rounded averages.
    return bool(results) and sum(item.score_basis_points for item in results) >= threshold * len(results)


@dataclass(frozen=True)
class PackCertificationDecision:
    status: str
    pack_identity: str
    policy_sha256: str
    evidence_truth_sha256: str | None
    aggregate_score_basis_points: int | None
    dimension_scores: tuple[tuple[str, int], ...]
    reasons: tuple[str, ...]
    producer: ProducerMetadata | None

    @property
    def certified(self) -> bool:
        return self.status == "certified"

    def as_dict(self) -> dict[str, object]:
        return {
            "aggregateScoreBasisPoints": self.aggregate_score_basis_points,
            "apiVersion": PACK_CERTIFICATION_DECISION_API_VERSION,
            "certified": self.certified,
            "dimensionScores": {key: value for key, value in self.dimension_scores},
            "evidenceTruthSha256": self.evidence_truth_sha256,
            "packIdentity": self.pack_identity,
            "policySha256": self.policy_sha256,
            "producer": None if self.producer is None else self.producer.as_dict(),
            "reasons": list(self.reasons),
            "status": self.status,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


def evaluate_pack_certification(
    manifest: PackManifest,
    content_sha256: str,
    policy: ResolvedPackCertificationPolicy,
    evidence: PackEvalEvidence | None,
    *,
    risk: str = "standard",
) -> PackCertificationDecision:
    if not _valid_hash(content_sha256):
        raise _fail("SDAI-PACK-CERT-001", "contentSha256 must be SHA-256")
    requirement = effective_requirement(policy, manifest, risk=risk)
    if evidence is None:
        status = "missing" if policy.require_certification else "not-required"
        return PackCertificationDecision(status, manifest.identity, policy.sha256, None, None, (), ("certification-required",) if policy.require_certification else (), None)

    stale: list[str] = []
    if evidence.pack_identity != manifest.identity:
        stale.append("pack-identity-changed")
    if evidence.manifest_sha256 != manifest.sha256:
        stale.append("manifest-changed")
    if evidence.content_sha256 != content_sha256:
        stale.append("content-changed")
    if evidence.policy_sha256 != policy.sha256:
        stale.append("policy-changed")
    if stale:
        return PackCertificationDecision("stale", manifest.identity, policy.sha256, evidence.truth_sha256, None, (), tuple(sorted(stale)), evidence.producer)

    by_id = {item.id: item for item in evidence.cases}
    reasons: list[str] = []
    for case_id in requirement.required_cases:
        case = by_id.get(case_id)
        if case is None:
            reasons.append(f"required-case-missing:{case_id}")
        elif not case.passed:
            reasons.append(f"required-case-failed:{case_id}")

    dimensions: dict[str, list[PackEvalCaseResult]] = {}
    for result in evidence.cases:
        dimensions.setdefault(result.dimension, []).append(result)
    dimension_scores: list[tuple[str, int]] = []
    for name in sorted(dimensions):
        values = tuple(dimensions[name])
        dimension_scores.append((name, sum(item.score_basis_points for item in values) // len(values)))
    for name, threshold in requirement.required_dimensions:
        values = tuple(dimensions.get(name, ()))
        if not values:
            reasons.append(f"required-dimension-missing:{name}")
        elif not _threshold_satisfied(values, threshold):
            reasons.append(f"dimension-score-below-minimum:{name}")

    if not _threshold_satisfied(evidence.cases, requirement.minimum_score_basis_points):
        reasons.append("aggregate-score-below-minimum")
    if any(not item.passed for item in evidence.cases if item.id in requirement.required_cases):
        # Required-case reason above is authoritative; no producer/model behavior can override it.
        pass

    aggregate = sum(item.score_basis_points for item in evidence.cases) // len(evidence.cases)
    status = "certified" if not reasons else "failed"
    return PackCertificationDecision(
        status=status,
        pack_identity=manifest.identity,
        policy_sha256=policy.sha256,
        evidence_truth_sha256=evidence.truth_sha256,
        aggregate_score_basis_points=aggregate,
        dimension_scores=tuple(dimension_scores),
        reasons=tuple(sorted(set(reasons))),
        producer=evidence.producer,
    )


def load_pack_certification_policy(path: Path) -> PackCertificationPolicy:
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-PACK-CERT-003", "Pack certification policy must be a regular non-symlink file")
    try:
        return PackCertificationPolicy.from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-PACK-CERT-003", "unable to read Pack certification policy as UTF-8") from exc


def load_pack_eval_evidence(path: Path) -> PackEvalEvidence:
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-PACK-CERT-003", "Pack eval evidence must be a regular non-symlink file")
    try:
        return PackEvalEvidence.from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-PACK-CERT-003", "unable to read Pack eval evidence as UTF-8") from exc
