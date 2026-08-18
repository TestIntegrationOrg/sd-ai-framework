from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from sdai.constitution import Constitution
from sdai.contracts import ContractDiffResult, ContractError, ContractFinding, ContractSeverity
from sdai.policy import OperatingMode, PolicyError, load_effective_configuration
from sdai.trace_evidence import (
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_freshness import EvidenceFreshnessReport, ProofFreshness


CONTRACT_POLICY_API_VERSION = "sdai.contract-policy/v1"
CONTRACT_POLICY_DECISION_API_VERSION = "sdai.contract-policy-decision/v1"
CONTRACT_POLICY_MAX_BYTES = 1024 * 1024
CONTRACT_POLICY_PATH = ".sdai/contract-policy.yaml"
ORG_CONTRACT_POLICY_ENV = "SDAI_ORG_CONTRACT_POLICY_PATH"
USER_CONTRACT_POLICY_ENV = "SDAI_USER_CONTRACT_POLICY_PATH"


class ContractChangeClass(StrEnum):
    NON_BREAKING = "non-breaking"
    BREAKING = "breaking"
    UNKNOWN = "unknown"


class ContractPolicyOutcome(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"


class ContractCriticality(StrEnum):
    LIGHT = "light"
    STANDARD = "standard"
    CRITICAL = "critical"


class ContractEvidenceType(StrEnum):
    ADR = "adr"
    ARCHITECTURE_APPROVAL = "architecture-approval"
    MIGRATION_PLAN = "migration-plan"


@dataclass(frozen=True, slots=True)
class ContractPolicyRule:
    allow_breaking: bool
    allow_unknown: bool
    required_evidence: tuple[ContractEvidenceType, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "allowBreaking": self.allow_breaking,
            "allowUnknown": self.allow_unknown,
            "requiredEvidence": [item.value for item in self.required_evidence],
        }


@dataclass(frozen=True, slots=True)
class _ContractPolicyRuleLayer:
    allow_breaking: bool | None = None
    allow_unknown: bool | None = None
    required_evidence: tuple[ContractEvidenceType, ...] = ()


@dataclass(frozen=True, slots=True)
class ContractPolicySource:
    source: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class _ContractPolicyLayer:
    source: ContractPolicySource
    rules: Mapping[ContractCriticality, _ContractPolicyRuleLayer]


@dataclass(frozen=True, slots=True)
class EffectiveContractPolicy:
    sources: tuple[ContractPolicySource, ...]
    rules: Mapping[ContractCriticality, ContractPolicyRule]
    sha256: str

    def rule_for(self, criticality: ContractCriticality | str) -> ContractPolicyRule:
        try:
            normalized = (
                criticality
                if isinstance(criticality, ContractCriticality)
                else ContractCriticality(criticality)
            )
        except ValueError as exc:
            raise ContractError(
                "SDAI-CONTRACT-POLICY-002",
                f"unsupported contract criticality: {criticality!r}",
            ) from exc
        return self.rules[normalized]

    def to_dict(self) -> dict[str, object]:
        return {
            "apiVersion": CONTRACT_POLICY_API_VERSION,
            "kind": "EffectiveContractPolicy",
            "sources": [item.to_dict() for item in self.sources],
            "rules": {item.value: self.rules[item].to_dict() for item in ContractCriticality},
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ContractFindingClassification:
    code: str
    severity: str
    change_class: ContractChangeClass
    compatibility: str
    pointer: str | None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "severity": self.severity,
            "class": self.change_class.value,
            "compatibility": self.compatibility,
        }
        if self.pointer is not None:
            payload["pointer"] = self.pointer
        return payload


@dataclass(frozen=True, slots=True)
class ContractEvidenceAssessment:
    evidence_id: str
    evidence_type: ContractEvidenceType | None
    evidence_sha256: str
    accepted: bool
    freshness: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "evidenceId": self.evidence_id,
            "evidenceType": self.evidence_type.value if self.evidence_type is not None else None,
            "evidenceSha256": self.evidence_sha256,
            "accepted": self.accepted,
            "freshness": self.freshness,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ContractPolicyDecision:
    criticality: ContractCriticality
    change_class: ContractChangeClass
    outcome: ContractPolicyOutcome
    baseline_sha256: str
    candidate_sha256: str
    diff_sha256: str
    policy_sha256: str
    constitution_sha256: str
    required_evidence: tuple[ContractEvidenceType, ...]
    classifications: tuple[ContractFindingClassification, ...]
    evidence: tuple[ContractEvidenceAssessment, ...]
    reasons: tuple[str, ...]
    sha256: str

    @property
    def allowed(self) -> bool:
        return self.outcome is ContractPolicyOutcome.ALLOWED

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "apiVersion": CONTRACT_POLICY_DECISION_API_VERSION,
            "kind": "ContractPolicyDecision",
            "criticality": self.criticality.value,
            "changeClass": self.change_class.value,
            "outcome": self.outcome.value,
            "allowed": self.allowed,
            "baselineSha256": self.baseline_sha256,
            "candidateSha256": self.candidate_sha256,
            "diffSha256": self.diff_sha256,
            "policySha256": self.policy_sha256,
            "constitutionSha256": self.constitution_sha256,
            "requiredEvidence": [item.value for item in self.required_evidence],
            "classifications": [item.to_dict() for item in self.classifications],
            "evidence": [item.to_dict() for item in self.evidence],
            "reasons": list(self.reasons),
        }

    def to_dict(self) -> dict[str, object]:
        payload = self._unsigned_dict()
        payload["sha256"] = self.sha256
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.to_dict()) + "\n"


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
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


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-001",
            f"contract policy data is not canonical finite JSON: {exc}",
        ) from exc


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _constitution_hash(value: Constitution | str) -> str:
    raw = value.sha256 if isinstance(value, Constitution) else value
    if not isinstance(raw, str):
        raise ContractError("SDAI-CONTRACT-POLICY-003", "constitution SHA-256 must be text")
    normalized = raw.strip().casefold()
    digest = normalized[7:] if normalized.startswith("sha256:") else normalized
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ContractError(
            "SDAI-CONTRACT-POLICY-003",
            f"invalid constitution SHA-256: {raw!r}",
        )
    return "sha256:" + digest


def _core_layer() -> _ContractPolicyLayer:
    rules: dict[ContractCriticality, _ContractPolicyRuleLayer] = {
        ContractCriticality.LIGHT: _ContractPolicyRuleLayer(
            allow_breaking=True,
            allow_unknown=False,
            required_evidence=(),
        ),
        ContractCriticality.STANDARD: _ContractPolicyRuleLayer(
            allow_breaking=True,
            allow_unknown=False,
            required_evidence=(ContractEvidenceType.MIGRATION_PLAN,),
        ),
        ContractCriticality.CRITICAL: _ContractPolicyRuleLayer(
            allow_breaking=True,
            allow_unknown=False,
            required_evidence=(
                ContractEvidenceType.ARCHITECTURE_APPROVAL,
                ContractEvidenceType.MIGRATION_PLAN,
            ),
        ),
    }
    payload = {
        "apiVersion": CONTRACT_POLICY_API_VERSION,
        "kind": "CoreContractPolicy",
        "rules": {
            key.value: {
                "allowBreaking": value.allow_breaking,
                "allowUnknown": value.allow_unknown,
                "requiredEvidence": [item.value for item in value.required_evidence],
            }
            for key, value in rules.items()
        },
    }
    return _ContractPolicyLayer(
        source=ContractPolicySource("core:sdai-0.16", _hash_json(payload)),
        rules=MappingProxyType(rules),
    )


def _safe_external_policy(path_text: str, *, label: str, project_root: Path) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        raise ContractError("SDAI-CONTRACT-POLICY-004", f"{label} must be an absolute path")
    resolved = path.resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ContractError(
            "SDAI-CONTRACT-POLICY-004",
            f"{label} must reference a regular non-symlink file: {resolved}",
        )
    if label == ORG_CONTRACT_POLICY_ENV:
        try:
            resolved.relative_to(project_root.resolve())
        except ValueError:
            pass
        else:
            raise ContractError(
                "SDAI-CONTRACT-POLICY-004",
                f"{label} must be managed outside the project repository",
            )
    return resolved


def _safe_repo_policy(project_root: Path) -> Path:
    root = project_root.resolve()
    candidate = root / CONTRACT_POLICY_PATH
    current = root
    for part in Path(CONTRACT_POLICY_PATH).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ContractError(
                "SDAI-CONTRACT-POLICY-004",
                f"repository contract policy contains a symlink component: {CONTRACT_POLICY_PATH}",
            )
    return candidate


def _read_policy(path: Path, *, source: str) -> tuple[bytes, Mapping[str, object]]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-004",
            f"unable to read {source}: {exc}",
        ) from exc
    if len(content) > CONTRACT_POLICY_MAX_BYTES:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-004",
            f"{source} exceeds the {CONTRACT_POLICY_MAX_BYTES}-byte limit",
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-004",
            f"{source} is not valid UTF-8",
        ) from exc
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-001",
            f"invalid {source}: {exc}",
        ) from exc
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise ContractError(
            "SDAI-CONTRACT-POLICY-001",
            f"{source} must be a string-keyed mapping",
        )
    return content, raw


def _parse_rule(
    value: object,
    *,
    source: str,
    criticality: ContractCriticality,
) -> _ContractPolicyRuleLayer:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ContractError(
            "SDAI-CONTRACT-POLICY-001",
            f"{source}: rules.{criticality.value} must be a mapping",
        )
    unknown = sorted(set(value) - {"allowBreaking", "allowUnknown", "requiredEvidence"})
    if unknown:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-001",
            f"{source}: rules.{criticality.value} contains unsupported key(s): {', '.join(unknown)}",
        )
    for key in ("allowBreaking", "allowUnknown"):
        if key in value and not isinstance(value[key], bool):
            raise ContractError(
                "SDAI-CONTRACT-POLICY-001",
                f"{source}: rules.{criticality.value}.{key} must be true or false",
            )
    raw_evidence = value.get("requiredEvidence", [])
    if not isinstance(raw_evidence, list) or not all(isinstance(item, str) for item in raw_evidence):
        raise ContractError(
            "SDAI-CONTRACT-POLICY-001",
            f"{source}: rules.{criticality.value}.requiredEvidence must be a string list",
        )
    evidence: set[ContractEvidenceType] = set()
    for item in raw_evidence:
        try:
            evidence.add(ContractEvidenceType(item))
        except ValueError as exc:
            raise ContractError(
                "SDAI-CONTRACT-POLICY-001",
                f"{source}: unsupported contract evidence type {item!r}",
            ) from exc
    return _ContractPolicyRuleLayer(
        allow_breaking=value.get("allowBreaking") if "allowBreaking" in value else None,
        allow_unknown=value.get("allowUnknown") if "allowUnknown" in value else None,
        required_evidence=tuple(sorted(evidence, key=lambda item: item.value)),
    )


def _load_policy_layer(path: Path, *, source: str) -> _ContractPolicyLayer:
    content, raw = _read_policy(path, source=source)
    unknown = sorted(set(raw) - {"apiVersion", "kind", "rules"})
    if unknown:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-001",
            f"{source} contains unsupported key(s): {', '.join(unknown)}",
        )
    if raw.get("apiVersion") != CONTRACT_POLICY_API_VERSION or raw.get("kind") != "ContractPolicy":
        raise ContractError(
            "SDAI-CONTRACT-POLICY-001",
            f"{source} must use apiVersion={CONTRACT_POLICY_API_VERSION} kind=ContractPolicy",
        )
    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, Mapping) or not raw_rules:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-001",
            f"{source}.rules must be a non-empty mapping",
        )
    rules: dict[ContractCriticality, _ContractPolicyRuleLayer] = {}
    for key, value in raw_rules.items():
        try:
            criticality = ContractCriticality(key)
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "SDAI-CONTRACT-POLICY-001",
                f"{source}: unsupported criticality {key!r}",
            ) from exc
        rules[criticality] = _parse_rule(value, source=source, criticality=criticality)
    return _ContractPolicyLayer(
        source=ContractPolicySource(source, _hash_bytes(content)),
        rules=MappingProxyType(rules),
    )


def merge_contract_policy_layers(layers: Sequence[_ContractPolicyLayer]) -> EffectiveContractPolicy:
    """Merge contract policy monotonically so lower layers can only tighten."""
    if not layers:
        raise ContractError("SDAI-CONTRACT-POLICY-001", "at least one contract policy layer is required")
    effective: dict[ContractCriticality, ContractPolicyRule] = {}
    for criticality in ContractCriticality:
        applicable = [layer.rules[criticality] for layer in layers if criticality in layer.rules]
        allow_breaking_values = [
            item.allow_breaking for item in applicable if item.allow_breaking is not None
        ]
        allow_unknown_values = [
            item.allow_unknown for item in applicable if item.allow_unknown is not None
        ]
        evidence: set[ContractEvidenceType] = set()
        for item in applicable:
            evidence.update(item.required_evidence)
        effective[criticality] = ContractPolicyRule(
            allow_breaking=all(allow_breaking_values) if allow_breaking_values else False,
            allow_unknown=all(allow_unknown_values) if allow_unknown_values else False,
            required_evidence=tuple(sorted(evidence, key=lambda item: item.value)),
        )
    sources = tuple(layer.source for layer in layers)
    unsigned = {
        "apiVersion": CONTRACT_POLICY_API_VERSION,
        "kind": "EffectiveContractPolicy",
        "sources": [item.to_dict() for item in sources],
        "rules": {item.value: effective[item].to_dict() for item in ContractCriticality},
    }
    return EffectiveContractPolicy(
        sources=sources,
        rules=MappingProxyType(effective),
        sha256=_hash_json(unsigned),
    )


def load_effective_contract_policy(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> EffectiveContractPolicy:
    """Load immutable core -> organization -> repository -> user restrictions."""
    root = project_root.resolve()
    env = dict(os.environ if environ is None else environ)
    try:
        base_policy = load_effective_configuration(root, environ=env)
    except PolicyError as exc:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-005",
            f"effective SDAI policy is invalid: {exc}",
        ) from exc

    layers: list[_ContractPolicyLayer] = [_core_layer()]
    org_value = env.get(ORG_CONTRACT_POLICY_ENV, "").strip()
    if org_value:
        path = _safe_external_policy(
            org_value,
            label=ORG_CONTRACT_POLICY_ENV,
            project_root=root,
        )
        # Stable semantic labels keep effective hashes machine/path independent.
        layers.append(_load_policy_layer(path, source="organization"))

    repo_path = _safe_repo_policy(root)
    if repo_path.exists():
        if repo_path.is_symlink() or not repo_path.is_file():
            raise ContractError(
                "SDAI-CONTRACT-POLICY-004",
                f"repository contract policy must be a regular non-symlink file: {CONTRACT_POLICY_PATH}",
            )
        layers.append(
            _load_policy_layer(
                repo_path,
                source=f"repository:{CONTRACT_POLICY_PATH}",
            )
        )

    user_value = env.get(USER_CONTRACT_POLICY_ENV, "").strip()
    if user_value:
        path = _safe_external_policy(
            user_value,
            label=USER_CONTRACT_POLICY_ENV,
            project_root=root,
        )
        layers.append(_load_policy_layer(path, source="user"))

    if base_policy.operating_mode is OperatingMode.ENTERPRISE and not base_policy.sources:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-005",
            "enterprise contract governance requires an effective organization policy",
        )
    return merge_contract_policy_layers(layers)


_OPENAPI_BREAKING = frozenset(
    {
        "SDAI-CONTRACT-OPENAPI-DIFF-001",
        "SDAI-CONTRACT-OPENAPI-DIFF-002",
        "SDAI-CONTRACT-OPENAPI-DIFF-010",
        "SDAI-CONTRACT-OPENAPI-DIFF-011",
        "SDAI-CONTRACT-OPENAPI-DIFF-012",
        "SDAI-CONTRACT-OPENAPI-DIFF-013",
        "SDAI-CONTRACT-OPENAPI-DIFF-014",
        *{f"SDAI-CONTRACT-OPENAPI-DIFF-{number:03d}" for number in range(20, 27)},
    }
)
_ASYNCAPI_BREAKING = frozenset(
    {
        *{f"SDAI-CONTRACT-ASYNCAPI-DIFF-{number:03d}" for number in range(1, 6)},
        *{f"SDAI-CONTRACT-ASYNCAPI-DIFF-{number:03d}" for number in range(20, 24)},
    }
)
_JSON_SCHEMA_BREAKING = frozenset(
    {"SDAI-CONTRACT-JSONSCHEMA-DIFF-001"}
    | {f"SDAI-CONTRACT-JSONSCHEMA-DIFF-{number:03d}" for number in range(10, 19)}
)
_PROTOBUF_BREAKING = frozenset(
    f"SDAI-CONTRACT-PROTOBUF-DIFF-{number:03d}" for number in range(1, 16)
)
_KNOWN_BREAKING_CODES = (
    _OPENAPI_BREAKING | _ASYNCAPI_BREAKING | _JSON_SCHEMA_BREAKING | _PROTOBUF_BREAKING
)


def classify_contract_finding(finding: ContractFinding) -> ContractFindingClassification:
    if finding.code in _KNOWN_BREAKING_CODES:
        change_class = ContractChangeClass.BREAKING
    elif finding.severity is ContractSeverity.ERROR:
        # Future diff codes and validation/parser errors remain unknown and fail closed.
        change_class = ContractChangeClass.UNKNOWN
    else:
        change_class = ContractChangeClass.NON_BREAKING
    pointer = finding.provenance.pointer if finding.provenance is not None else None
    return ContractFindingClassification(
        code=finding.code,
        severity=finding.severity.value,
        change_class=change_class,
        compatibility=finding.compatibility.value,
        pointer=pointer,
    )


def classify_contract_diff(
    diff: ContractDiffResult,
) -> tuple[ContractChangeClass, tuple[ContractFindingClassification, ...]]:
    classifications = tuple(
        sorted(
            (classify_contract_finding(item) for item in diff.findings),
            key=lambda item: (
                item.change_class.value,
                item.severity,
                item.code,
                item.pointer or "",
                item.compatibility,
            ),
        )
    )
    classes = {item.change_class for item in classifications}
    if ContractChangeClass.UNKNOWN in classes:
        overall = ContractChangeClass.UNKNOWN
    elif ContractChangeClass.BREAKING in classes:
        overall = ContractChangeClass.BREAKING
    else:
        overall = ContractChangeClass.NON_BREAKING
    return overall, classifications


def _contract_claim(
    record: TraceEvidence,
) -> tuple[ContractEvidenceType | None, Mapping[str, object] | None, list[str]]:
    reasons: list[str] = []
    raw = record.result.get("contractPolicy") if isinstance(record.result, Mapping) else None
    if not isinstance(raw, Mapping):
        return None, None, ["evidence result.contractPolicy claim is missing"]
    expected = {
        "evidenceType",
        "baselineSha256",
        "candidateSha256",
        "diffSha256",
        "policySha256",
        "constitutionSha256",
    }
    if set(raw) != expected:
        reasons.append("evidence contractPolicy claim fields do not match the v1 binding contract")
        return None, raw, reasons
    raw_type = raw.get("evidenceType")
    try:
        evidence_type = ContractEvidenceType(raw_type)
    except (TypeError, ValueError):
        reasons.append(f"unsupported contract evidence type: {raw_type!r}")
        return None, raw, reasons
    return evidence_type, raw, reasons


def _assess_evidence(
    record: TraceEvidence,
    freshness: EvidenceFreshnessReport | None,
    *,
    diff: ContractDiffResult,
    policy_sha256: str,
    constitution_sha256: str,
) -> ContractEvidenceAssessment:
    evidence_type, claim, reasons = _contract_claim(record)
    freshness_value = (
        freshness.freshness.value if freshness is not None else ProofFreshness.MISSING.value
    )
    if freshness is None:
        reasons.append("no current trace freshness report exists for evidence")
    else:
        if freshness.evidence_id != record.evidence_id:
            reasons.append("freshness report evidence identity does not match the evidence record")
        if freshness.subject != record.subject:
            reasons.append("freshness report subject does not match the evidence record")
        if freshness.evidence_git_commit != record.git_commit:
            reasons.append("freshness report Git commit does not match the evidence record")
        if freshness.freshness is not ProofFreshness.VALID:
            reasons.append(f"trace evidence is not fresh: {freshness.freshness.value}")
            reasons.extend(freshness.reasons)

    if record.status not in {EvidenceStatus.PASSED, EvidenceStatus.RECORDED}:
        reasons.append(f"evidence status is not successful: {record.status.value}")

    if claim is not None and evidence_type is not None:
        expected_bindings = {
            "baselineSha256": diff.before.sha256,
            "candidateSha256": diff.after.sha256,
            "diffSha256": diff.sha256,
            "policySha256": policy_sha256,
            "constitutionSha256": constitution_sha256,
        }
        for key, expected in expected_bindings.items():
            if claim.get(key) != expected:
                reasons.append(f"{key} does not match the current governed contract input")

        if evidence_type is ContractEvidenceType.ARCHITECTURE_APPROVAL:
            if record.kind is not EvidenceKind.APPROVAL:
                reasons.append("architecture approval evidence must use evidence kind 'approval'")
            if record.producer.semantic_role != "architecture-approver":
                reasons.append(
                    "architecture approval must be produced by semantic role 'architecture-approver'"
                )
            if record.producer.provider is not None or record.producer.model is not None:
                reasons.append("architecture approval cannot be self-approved by an AI provider/model")
        elif record.kind not in {EvidenceKind.REVIEW, EvidenceKind.APPROVAL}:
            reasons.append(f"{evidence_type.value} evidence must use kind 'review' or 'approval'")

        if evidence_type in {ContractEvidenceType.ADR, ContractEvidenceType.MIGRATION_PLAN} and not any(
            binding.kind is EvidenceBindingKind.ARTIFACT for binding in record.bindings
        ):
            reasons.append(f"{evidence_type.value} requires at least one artifact content binding")

    return ContractEvidenceAssessment(
        evidence_id=record.evidence_id,
        evidence_type=evidence_type,
        evidence_sha256=record.sha256,
        accepted=not reasons,
        freshness=freshness_value,
        reasons=tuple(sorted(set(reasons))),
    )


def evaluate_contract_policy(
    diff: ContractDiffResult,
    policy: EffectiveContractPolicy,
    *,
    criticality: ContractCriticality | str,
    constitution: Constitution | str,
    evidence: Sequence[TraceEvidence] = (),
    freshness_reports: Mapping[str, EvidenceFreshnessReport] | None = None,
) -> ContractPolicyDecision:
    """Classify a diff and apply deterministic, hash-bound governance policy."""
    if not isinstance(diff, ContractDiffResult):
        raise ContractError("SDAI-CONTRACT-POLICY-003", "diff must be ContractDiffResult")
    if not isinstance(policy, EffectiveContractPolicy):
        raise ContractError("SDAI-CONTRACT-POLICY-003", "policy must be EffectiveContractPolicy")
    try:
        normalized_criticality = (
            criticality
            if isinstance(criticality, ContractCriticality)
            else ContractCriticality(criticality)
        )
    except ValueError as exc:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-002",
            f"unsupported contract criticality: {criticality!r}",
        ) from exc

    constitution_sha256 = _constitution_hash(constitution)
    change_class, classifications = classify_contract_diff(diff)
    rule = policy.rule_for(normalized_criticality)
    reports = freshness_reports or {}
    assessments = tuple(
        sorted(
            (
                _assess_evidence(
                    item,
                    reports.get(item.evidence_id),
                    diff=diff,
                    policy_sha256=policy.sha256,
                    constitution_sha256=constitution_sha256,
                )
                for item in evidence
            ),
            key=lambda item: (
                item.evidence_type.value if item.evidence_type is not None else "~",
                item.evidence_id,
            ),
        )
    )

    reasons: list[str] = []
    if change_class is ContractChangeClass.NON_BREAKING:
        outcome = ContractPolicyOutcome.ALLOWED
        reasons.append("contract change is deterministically non-breaking")
    elif change_class is ContractChangeClass.UNKNOWN:
        if rule.allow_unknown:
            outcome = ContractPolicyOutcome.ALLOWED
            reasons.append("unknown contract change is explicitly allowed by effective policy")
        else:
            outcome = ContractPolicyOutcome.BLOCKED
            reasons.append("unknown contract change is blocked by effective policy")
    elif not rule.allow_breaking:
        outcome = ContractPolicyOutcome.BLOCKED
        reasons.append("breaking contract changes are disabled by effective policy")
    else:
        accepted_types = {
            item.evidence_type
            for item in assessments
            if item.accepted and item.evidence_type is not None
        }
        missing = [item for item in rule.required_evidence if item not in accepted_types]
        if missing:
            outcome = ContractPolicyOutcome.BLOCKED
            reasons.append(
                "missing fresh hash-bound evidence: "
                + ", ".join(item.value for item in missing)
            )
        else:
            outcome = ContractPolicyOutcome.ALLOWED
            if rule.required_evidence:
                reasons.append("all required fresh hash-bound evidence is satisfied")
            else:
                reasons.append(
                    "breaking contract change is allowed without additional evidence at this criticality"
                )

    required_evidence = (
        rule.required_evidence if change_class is ContractChangeClass.BREAKING else ()
    )
    canonical_reasons = tuple(sorted(set(reasons)))
    unsigned = {
        "apiVersion": CONTRACT_POLICY_DECISION_API_VERSION,
        "kind": "ContractPolicyDecision",
        "criticality": normalized_criticality.value,
        "changeClass": change_class.value,
        "outcome": outcome.value,
        "allowed": outcome is ContractPolicyOutcome.ALLOWED,
        "baselineSha256": diff.before.sha256,
        "candidateSha256": diff.after.sha256,
        "diffSha256": diff.sha256,
        "policySha256": policy.sha256,
        "constitutionSha256": constitution_sha256,
        "requiredEvidence": [item.value for item in required_evidence],
        "classifications": [item.to_dict() for item in classifications],
        "evidence": [item.to_dict() for item in assessments],
        "reasons": list(canonical_reasons),
    }
    return ContractPolicyDecision(
        criticality=normalized_criticality,
        change_class=change_class,
        outcome=outcome,
        baseline_sha256=diff.before.sha256,
        candidate_sha256=diff.after.sha256,
        diff_sha256=diff.sha256,
        policy_sha256=policy.sha256,
        constitution_sha256=constitution_sha256,
        required_evidence=required_evidence,
        classifications=classifications,
        evidence=assessments,
        reasons=canonical_reasons,
        sha256=_hash_json(unsigned),
    )


def contract_policy_exit_code(decision: ContractPolicyDecision) -> int:
    """Stable CI exit class: 0 allowed, 2 policy-blocked."""
    return 0 if decision.allowed else 2
