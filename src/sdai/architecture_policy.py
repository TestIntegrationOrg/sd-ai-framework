from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from sdai.architecture_drift import (
    ArchitectureDriftError,
    ArchitectureDriftFinding,
    ArchitectureDriftReport,
    ArchitectureDriftSeverity,
    ArchitectureFactKind,
)


ARCHITECTURE_POLICY_API_VERSION = "sdai.architecture-drift-policy/v1"
ARCHITECTURE_POLICY_DECISION_API_VERSION = "sdai.architecture-drift-policy-decision/v1"
ARCHITECTURE_POLICY_PATH = ".sdai/architecture-drift-policy.yaml"
ARCHITECTURE_POLICY_MAX_BYTES = 1024 * 1024
ORG_ARCHITECTURE_POLICY_ENV = "SDAI_ORG_ARCHITECTURE_DRIFT_POLICY_PATH"
USER_ARCHITECTURE_POLICY_ENV = "SDAI_USER_ARCHITECTURE_DRIFT_POLICY_PATH"


class ArchitecturePolicyError(ArchitectureDriftError):
    """Raised when architecture-drift governance cannot be evaluated safely."""


@dataclass(frozen=True, slots=True)
class ArchitecturePolicySource:
    source: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class _ArchitecturePolicyLayer:
    source: ArchitecturePolicySource
    required: bool | None
    default_threshold: ArchitectureDriftSeverity | None
    kind_thresholds: Mapping[ArchitectureFactKind, ArchitectureDriftSeverity]


@dataclass(frozen=True, slots=True)
class EffectiveArchitecturePolicy:
    sources: tuple[ArchitecturePolicySource, ...]
    required: bool
    default_threshold: ArchitectureDriftSeverity
    kind_thresholds: Mapping[ArchitectureFactKind, ArchitectureDriftSeverity]
    sha256: str

    def threshold_for(self, kind: ArchitectureFactKind | str) -> ArchitectureDriftSeverity:
        try:
            normalized = kind if isinstance(kind, ArchitectureFactKind) else ArchitectureFactKind(kind)
        except ValueError as exc:
            raise ArchitecturePolicyError(f"SDAI-ARCH-POLICY-002: unsupported architecture fact kind: {kind!r}") from exc
        specific = self.kind_thresholds.get(normalized)
        if specific is None:
            return self.default_threshold
        return _stricter_threshold(self.default_threshold, specific)

    def blocks(self, finding: ArchitectureDriftFinding) -> bool:
        if not isinstance(finding, ArchitectureDriftFinding):
            raise ArchitecturePolicyError("SDAI-ARCH-POLICY-002: policy requires a validated architecture drift finding")
        threshold = self.threshold_for(finding.kind)
        return _severity_rank(finding.severity) >= _severity_rank(threshold)

    def to_dict(self) -> dict[str, object]:
        return {
            "apiVersion": ARCHITECTURE_POLICY_API_VERSION,
            "kind": "EffectiveArchitectureDriftPolicy",
            "sources": [item.to_dict() for item in self.sources],
            "required": self.required,
            "defaultThreshold": self.default_threshold.value,
            "kinds": {
                kind.value: self.kind_thresholds[kind].value
                for kind in sorted(self.kind_thresholds, key=lambda item: item.value)
            },
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ArchitecturePolicyBlocker:
    code: str
    kind: str | None
    severity: str | None
    source: str | None
    target: str | None
    approved_fact_id: str | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "kind": self.kind,
            "severity": self.severity,
            "source": self.source,
            "target": self.target,
            "approvedFactId": self.approved_fact_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ArchitecturePolicyDecision:
    feature_id: str
    topology_present: bool
    outcome: str
    policy_sha256: str
    report_sha256: str | None
    blockers: tuple[ArchitecturePolicyBlocker, ...]
    reasons: tuple[str, ...]
    sha256: str

    @property
    def blocked(self) -> bool:
        return self.outcome == "blocked"

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "apiVersion": ARCHITECTURE_POLICY_DECISION_API_VERSION,
            "kind": "ArchitectureDriftPolicyDecision",
            "featureId": self.feature_id,
            "topologyPresent": self.topology_present,
            "outcome": self.outcome,
            "blocked": self.blocked,
            "policySha256": self.policy_sha256,
            "reportSha256": self.report_sha256,
            "blockers": [item.to_dict() for item in self.blockers],
            "reasons": list(self.reasons),
        }

    def to_dict(self) -> dict[str, object]:
        value = self._unsigned_dict()
        value["sha256"] = self.sha256
        return value

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
        raise ArchitecturePolicyError(f"SDAI-ARCH-POLICY-001: architecture policy data is not canonical JSON: {exc}") from exc


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _severity_rank(value: ArchitectureDriftSeverity) -> int:
    return {
        ArchitectureDriftSeverity.WARNING: 0,
        ArchitectureDriftSeverity.ERROR: 1,
    }[value]


def _stricter_threshold(
    left: ArchitectureDriftSeverity,
    right: ArchitectureDriftSeverity,
) -> ArchitectureDriftSeverity:
    return left if _severity_rank(left) <= _severity_rank(right) else right


def _core_layer() -> _ArchitecturePolicyLayer:
    payload = {
        "apiVersion": ARCHITECTURE_POLICY_API_VERSION,
        "kind": "CoreArchitectureDriftPolicy",
        "required": False,
        "defaultThreshold": ArchitectureDriftSeverity.ERROR.value,
        "kinds": {},
    }
    return _ArchitecturePolicyLayer(
        source=ArchitecturePolicySource("core:sdai-0.17", _hash_json(payload)),
        required=False,
        default_threshold=ArchitectureDriftSeverity.ERROR,
        kind_thresholds=MappingProxyType({}),
    )


def _safe_repo_policy(root: Path) -> Path:
    candidate = root / ARCHITECTURE_POLICY_PATH
    current = root
    for part in Path(ARCHITECTURE_POLICY_PATH).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ArchitecturePolicyError(
                f"SDAI-ARCH-POLICY-004: repository architecture policy contains a symlink component: {ARCHITECTURE_POLICY_PATH}"
            )
    return candidate


def _safe_external_policy(path_text: str, *, label: str, project_root: Path) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        raise ArchitecturePolicyError(f"SDAI-ARCH-POLICY-004: {label} must be an absolute path")
    resolved = path.resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ArchitecturePolicyError(
            f"SDAI-ARCH-POLICY-004: {label} must reference a regular non-symlink file: {resolved}"
        )
    if label == ORG_ARCHITECTURE_POLICY_ENV:
        try:
            resolved.relative_to(project_root.resolve())
        except ValueError:
            pass
        else:
            raise ArchitecturePolicyError(
                f"SDAI-ARCH-POLICY-004: {label} must be managed outside the project repository"
            )
    return resolved


def _read_policy(path: Path, *, source: str) -> tuple[bytes, Mapping[str, object]]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ArchitecturePolicyError(f"SDAI-ARCH-POLICY-004: unable to read {source}: {exc}") from exc
    if len(content) > ARCHITECTURE_POLICY_MAX_BYTES:
        raise ArchitecturePolicyError(
            f"SDAI-ARCH-POLICY-004: {source} exceeds the {ARCHITECTURE_POLICY_MAX_BYTES}-byte limit"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchitecturePolicyError(f"SDAI-ARCH-POLICY-004: {source} is not valid UTF-8") from exc
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ArchitecturePolicyError(f"SDAI-ARCH-POLICY-001: invalid {source}: {exc}") from exc
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise ArchitecturePolicyError(f"SDAI-ARCH-POLICY-001: {source} must be a string-keyed mapping")
    return content, raw


def _threshold(value: object, *, source: str, label: str) -> ArchitectureDriftSeverity:
    if not isinstance(value, str):
        raise ArchitecturePolicyError(f"SDAI-ARCH-POLICY-001: {source}.{label} must be warning or error")
    try:
        return ArchitectureDriftSeverity(value)
    except ValueError as exc:
        raise ArchitecturePolicyError(
            f"SDAI-ARCH-POLICY-001: {source}.{label} must be warning or error"
        ) from exc


def _load_policy_layer(path: Path, *, source: str) -> _ArchitecturePolicyLayer:
    content, raw = _read_policy(path, source=source)
    expected = {"apiVersion", "kind", "required", "defaultThreshold", "kinds"}
    unknown = sorted(set(raw) - expected)
    if unknown:
        raise ArchitecturePolicyError(
            f"SDAI-ARCH-POLICY-001: {source} contains unsupported key(s): {', '.join(unknown)}"
        )
    if raw.get("apiVersion") != ARCHITECTURE_POLICY_API_VERSION or raw.get("kind") != "ArchitectureDriftPolicy":
        raise ArchitecturePolicyError(
            f"SDAI-ARCH-POLICY-001: {source} must use apiVersion={ARCHITECTURE_POLICY_API_VERSION} kind=ArchitectureDriftPolicy"
        )
    required = raw.get("required") if "required" in raw else None
    if required is not None and not isinstance(required, bool):
        raise ArchitecturePolicyError(f"SDAI-ARCH-POLICY-001: {source}.required must be true or false")
    default_threshold = (
        _threshold(raw["defaultThreshold"], source=source, label="defaultThreshold")
        if "defaultThreshold" in raw
        else None
    )
    raw_kinds = raw.get("kinds", {})
    if not isinstance(raw_kinds, Mapping) or not all(isinstance(key, str) for key in raw_kinds):
        raise ArchitecturePolicyError(f"SDAI-ARCH-POLICY-001: {source}.kinds must be a mapping")
    kinds: dict[ArchitectureFactKind, ArchitectureDriftSeverity] = {}
    for key, value in raw_kinds.items():
        try:
            kind = ArchitectureFactKind(key)
        except ValueError as exc:
            raise ArchitecturePolicyError(
                f"SDAI-ARCH-POLICY-001: {source}.kinds contains unsupported category {key!r}"
            ) from exc
        kinds[kind] = _threshold(value, source=source, label=f"kinds.{key}")
    return _ArchitecturePolicyLayer(
        source=ArchitecturePolicySource(source, _hash_bytes(content)),
        required=required,
        default_threshold=default_threshold,
        kind_thresholds=MappingProxyType(kinds),
    )


def merge_architecture_policy_layers(
    layers: Sequence[_ArchitecturePolicyLayer],
) -> EffectiveArchitecturePolicy:
    """Merge policy monotonically; later layers may only strengthen inherited governance."""
    if not layers:
        raise ArchitecturePolicyError("SDAI-ARCH-POLICY-001: at least one architecture policy layer is required")
    required = any(layer.required is True for layer in layers)
    defaults = [layer.default_threshold for layer in layers if layer.default_threshold is not None]
    default = ArchitectureDriftSeverity.ERROR
    for item in defaults:
        assert item is not None
        default = _stricter_threshold(default, item)

    kind_thresholds: dict[ArchitectureFactKind, ArchitectureDriftSeverity] = {}
    for kind in ArchitectureFactKind:
        threshold = default
        explicit = False
        for layer in layers:
            value = layer.kind_thresholds.get(kind)
            if value is None:
                continue
            explicit = True
            threshold = _stricter_threshold(threshold, value)
        if explicit and threshold != default:
            kind_thresholds[kind] = threshold

    sources = tuple(layer.source for layer in layers)
    unsigned = {
        "apiVersion": ARCHITECTURE_POLICY_API_VERSION,
        "kind": "EffectiveArchitectureDriftPolicy",
        "sources": [item.to_dict() for item in sources],
        "required": required,
        "defaultThreshold": default.value,
        "kinds": {
            kind.value: kind_thresholds[kind].value
            for kind in sorted(kind_thresholds, key=lambda item: item.value)
        },
    }
    return EffectiveArchitecturePolicy(
        sources=sources,
        required=required,
        default_threshold=default,
        kind_thresholds=MappingProxyType(kind_thresholds),
        sha256=_hash_json(unsigned),
    )


def load_effective_architecture_policy(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> EffectiveArchitecturePolicy:
    root = project_root.resolve()
    env = dict(os.environ if environ is None else environ)
    layers: list[_ArchitecturePolicyLayer] = [_core_layer()]

    org_value = env.get(ORG_ARCHITECTURE_POLICY_ENV, "").strip()
    if org_value:
        layers.append(
            _load_policy_layer(
                _safe_external_policy(org_value, label=ORG_ARCHITECTURE_POLICY_ENV, project_root=root),
                source="organization",
            )
        )

    repo = _safe_repo_policy(root)
    if repo.exists():
        if repo.is_symlink() or not repo.is_file():
            raise ArchitecturePolicyError(
                f"SDAI-ARCH-POLICY-004: repository architecture policy must be a regular non-symlink file: {ARCHITECTURE_POLICY_PATH}"
            )
        layers.append(_load_policy_layer(repo, source=f"repository:{ARCHITECTURE_POLICY_PATH}"))

    user_value = env.get(USER_ARCHITECTURE_POLICY_ENV, "").strip()
    if user_value:
        layers.append(
            _load_policy_layer(
                _safe_external_policy(user_value, label=USER_ARCHITECTURE_POLICY_ENV, project_root=root),
                source="user",
            )
        )
    return merge_architecture_policy_layers(layers)


def evaluate_architecture_policy(
    feature_id: str,
    policy: EffectiveArchitecturePolicy,
    report: ArchitectureDriftReport | None,
    *,
    topology_present: bool,
    governance_error: str | None = None,
) -> ArchitecturePolicyDecision:
    blockers: list[ArchitecturePolicyBlocker] = []
    reasons: list[str] = []

    if not topology_present and policy.required:
        reason = "effective architecture policy requires an approved topology"
        blockers.append(
            ArchitecturePolicyBlocker(
                "ARCH-POLICY-TOPOLOGY-REQUIRED",
                None,
                None,
                None,
                None,
                None,
                reason,
            )
        )
        reasons.append(reason)

    if governance_error is not None:
        reason = governance_error.strip() or "architecture governance evidence is not current"
        blockers.append(
            ArchitecturePolicyBlocker(
                "ARCH-POLICY-APPROVAL-INVALID",
                None,
                None,
                None,
                None,
                None,
                reason,
            )
        )
        reasons.append(reason)

    if report is not None:
        for finding in report.findings:
            if not policy.blocks(finding):
                continue
            threshold = policy.threshold_for(finding.kind)
            reason = (
                f"{finding.code} {finding.kind.value} severity={finding.severity.value} "
                f"meets blocking threshold={threshold.value}"
            )
            blockers.append(
                ArchitecturePolicyBlocker(
                    code=finding.code,
                    kind=finding.kind.value,
                    severity=finding.severity.value,
                    source=finding.source,
                    target=finding.target,
                    approved_fact_id=finding.approved_fact_id,
                    reason=reason,
                )
            )
            reasons.append(reason)

    blockers_tuple = tuple(
        sorted(
            blockers,
            key=lambda item: (
                item.code,
                item.kind or "",
                item.severity or "",
                item.source or "",
                item.target or "",
                item.approved_fact_id or "",
                item.reason,
            ),
        )
    )
    reasons_tuple = tuple(sorted(set(reasons)))
    outcome = "blocked" if blockers_tuple else "allowed"
    unsigned = {
        "apiVersion": ARCHITECTURE_POLICY_DECISION_API_VERSION,
        "kind": "ArchitectureDriftPolicyDecision",
        "featureId": feature_id,
        "topologyPresent": topology_present,
        "outcome": outcome,
        "blocked": bool(blockers_tuple),
        "policySha256": policy.sha256,
        "reportSha256": report.sha256 if report is not None else None,
        "blockers": [item.to_dict() for item in blockers_tuple],
        "reasons": list(reasons_tuple),
    }
    return ArchitecturePolicyDecision(
        feature_id=feature_id,
        topology_present=topology_present,
        outcome=outcome,
        policy_sha256=policy.sha256,
        report_sha256=report.sha256 if report is not None else None,
        blockers=blockers_tuple,
        reasons=reasons_tuple,
        sha256=_hash_json(unsigned),
    )


__all__ = [
    "ARCHITECTURE_POLICY_API_VERSION",
    "ARCHITECTURE_POLICY_DECISION_API_VERSION",
    "ARCHITECTURE_POLICY_PATH",
    "ORG_ARCHITECTURE_POLICY_ENV",
    "USER_ARCHITECTURE_POLICY_ENV",
    "ArchitecturePolicyBlocker",
    "ArchitecturePolicyDecision",
    "ArchitecturePolicyError",
    "ArchitecturePolicySource",
    "EffectiveArchitecturePolicy",
    "evaluate_architecture_policy",
    "load_effective_architecture_policy",
    "merge_architecture_policy_layers",
]
