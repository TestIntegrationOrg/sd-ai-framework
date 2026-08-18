from __future__ import annotations

from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import Iterable, Mapping

from sdai.architecture_drift import (
    ApprovedArchitecture,
    ArchitectureDriftError,
    ArchitectureDriftFinding,
    ArchitectureDriftReport,
    ArchitectureDriftSeverity,
    ArchitectureFact,
    ArchitectureFactKind,
    ArchitectureFactMode,
    ArchitectureObservation,
    ObservedArchitectureFact,
    compare_architecture,
)
from sdai.trace_graph import TraceProvenance


TRUST_BOUNDARY_OBSERVER_ID = "trust-boundary-security"
_EXTERNAL_SUBJECT = "external:public"
_EXTERNAL_ZONE = "zone:external"

SECURITY_DRIFT_SEVERITY_CANDIDATES: Mapping[str, ArchitectureDriftSeverity] = MappingProxyType(
    {
        "ARCH-SEC-ZONE-MISSING": ArchitectureDriftSeverity.ERROR,
        "ARCH-SEC-BOUNDARY-FORBIDDEN": ArchitectureDriftSeverity.ERROR,
        "ARCH-SEC-BOUNDARY-UNAPPROVED": ArchitectureDriftSeverity.ERROR,
        "ARCH-SEC-GATEWAY-BYPASS": ArchitectureDriftSeverity.ERROR,
        "ARCH-SEC-CONTROL-MISSING": ArchitectureDriftSeverity.ERROR,
        "ARCH-SEC-SENSITIVE-DATA-CROSSING": ArchitectureDriftSeverity.ERROR,
        "ARCH-SEC-DIRECTION-CHANGE": ArchitectureDriftSeverity.ERROR,
        "ARCH-SEC-EXPOSURE-CHANGE": ArchitectureDriftSeverity.ERROR,
        "ARCH-SEC-REQUIRED-MISSING": ArchitectureDriftSeverity.ERROR,
    }
)

_ALLOWED_EVIDENCE_KINDS = frozenset({"communication", "data-access", "deployment"})
_ALLOWED_DIRECTIONS = frozenset({"inbound", "outbound", "placement"})
_ALLOWED_MEMBERSHIP_FIELDS = frozenset({"role", "sensitive", "exposure"})
_ALLOWED_RULE_FIELDS = frozenset(
    {
        "role",
        "evidenceKind",
        "direction",
        "protocol",
        "access",
        "gateway",
        "requiredControl",
        "allowSensitiveData",
    }
)
_ALLOWED_CONTROL_FIELDS = frozenset({"role", "targetSubject"})


@dataclass(frozen=True, slots=True)
class _Membership:
    subject: str
    zone: str
    sensitive: bool
    exposure: str | None
    fact: ArchitectureFact


@dataclass(frozen=True, slots=True)
class _BoundaryRule:
    fact: ArchitectureFact
    from_zone: str
    to_zone: str
    evidence_kind: str
    direction: str
    protocol: str | None
    access: str | None
    gateway: str | None
    required_control: str | None
    allow_sensitive_data: bool


@dataclass(frozen=True, slots=True)
class _Crossing:
    source_subject: str
    target_subject: str
    from_zone: str
    to_zone: str
    evidence_kind: str
    direction: str
    protocol: str | None
    access: str | None
    sensitive_data: bool
    provenance: tuple[TraceProvenance, ...]

    def observed_fact(self) -> ObservedArchitectureFact:
        attributes: dict[str, object] = {
            "role": "crossing",
            "fromZone": self.from_zone,
            "toZone": self.to_zone,
            "evidenceKind": self.evidence_kind,
            "direction": self.direction,
        }
        if self.protocol is not None:
            attributes["protocol"] = self.protocol
        if self.access is not None:
            attributes["access"] = self.access
        if self.sensitive_data:
            attributes["sensitiveData"] = True
        return ObservedArchitectureFact(
            kind=ArchitectureFactKind.TRUST_BOUNDARY,
            source=self.source_subject,
            target=self.target_subject,
            attributes=attributes,
            provenance=self.provenance,
        )


def _fail(code: str, message: str) -> ArchitectureDriftError:
    return ArchitectureDriftError(f"{code}: {message}")


def _attributes(value: Mapping[str, object]) -> dict[str, object]:
    return json.loads(
        json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def _canonical_provenance(values: Iterable[TraceProvenance]) -> tuple[TraceProvenance, ...]:
    by_location: dict[tuple[str, int], TraceProvenance] = {}
    for value in values:
        previous = by_location.get(value.location)
        if previous is None:
            by_location[value.location] = value
            continue
        if previous == value:
            continue
        previous_key = (previous.declaration_sha256 or "", previous.detail or "")
        value_key = (value.declaration_sha256 or "", value.detail or "")
        by_location[value.location] = min((previous, value), key=lambda item: (item.declaration_sha256 or "", item.detail or ""))
    return tuple(
        sorted(
            by_location.values(),
            key=lambda item: (
                item.source.casefold(),
                item.source,
                item.line,
                item.declaration_sha256 or "",
                item.detail or "",
            ),
        )
    )


def _topology_provenance(approved: ApprovedArchitecture, detail: str) -> tuple[TraceProvenance, ...]:
    return (
        TraceProvenance(
            approved.topology.source,
            1,
            declaration_sha256=approved.topology.file_sha256,
            detail=detail,
        ),
    )


def _text_attribute(attributes: Mapping[str, object], name: str, *, required: bool = False) -> str | None:
    value = attributes.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise _fail("SDAI-ARCH-SECURITY-002", f"trust-boundary attribute {name!r} must be bounded non-empty text")
    return value


def _bool_attribute(attributes: Mapping[str, object], name: str, *, default: bool = False) -> bool:
    value = attributes.get(name, default)
    if not isinstance(value, bool):
        raise _fail("SDAI-ARCH-SECURITY-002", f"trust-boundary attribute {name!r} must be boolean")
    return value


def _parse_security_topology(
    approved: ApprovedArchitecture,
) -> tuple[dict[str, _Membership], tuple[_BoundaryRule, ...]]:
    memberships_by_subject: dict[str, list[_Membership]] = {}
    rules: list[_BoundaryRule] = []
    component_ids = {component.component_id for component in approved.topology.components}

    for fact in approved.topology.facts:
        if fact.kind is not ArchitectureFactKind.TRUST_BOUNDARY:
            continue
        attributes = _attributes(fact.attributes)
        role = attributes.get("role")
        if role == "zone-membership":
            if set(attributes) - _ALLOWED_MEMBERSHIP_FIELDS:
                raise _fail(
                    "SDAI-ARCH-SECURITY-002",
                    f"zone membership {fact.fact_id!r} contains unsupported attributes",
                )
            if fact.mode is ArchitectureFactMode.FORBIDDEN:
                raise _fail("SDAI-ARCH-SECURITY-002", f"zone membership {fact.fact_id!r} cannot be forbidden")
            if not fact.target.startswith("zone:"):
                raise _fail("SDAI-ARCH-SECURITY-002", f"zone membership {fact.fact_id!r} target must start with 'zone:'")
            exposure = _text_attribute(attributes, "exposure")
            membership = _Membership(
                subject=fact.source,
                zone=fact.target,
                sensitive=_bool_attribute(attributes, "sensitive", default=False),
                exposure=exposure,
                fact=fact,
            )
            memberships_by_subject.setdefault(fact.source, []).append(membership)
            continue

        if role == "boundary-rule":
            if set(attributes) - _ALLOWED_RULE_FIELDS:
                raise _fail(
                    "SDAI-ARCH-SECURITY-002",
                    f"boundary rule {fact.fact_id!r} contains unsupported attributes",
                )
            if not fact.source.startswith("zone:") or not fact.target.startswith("zone:"):
                raise _fail(
                    "SDAI-ARCH-SECURITY-002",
                    f"boundary rule {fact.fact_id!r} source/target must be zone identities",
                )
            evidence_kind = _text_attribute(attributes, "evidenceKind", required=True)
            direction = _text_attribute(attributes, "direction", required=True)
            assert evidence_kind is not None and direction is not None
            if evidence_kind not in _ALLOWED_EVIDENCE_KINDS:
                raise _fail("SDAI-ARCH-SECURITY-002", f"boundary rule {fact.fact_id!r} has unsupported evidenceKind")
            if direction not in _ALLOWED_DIRECTIONS:
                raise _fail("SDAI-ARCH-SECURITY-002", f"boundary rule {fact.fact_id!r} has unsupported direction")
            gateway = _text_attribute(attributes, "gateway")
            required_control = _text_attribute(attributes, "requiredControl")
            if gateway is not None and gateway not in component_ids:
                raise _fail(
                    "SDAI-ARCH-SECURITY-002",
                    f"boundary rule {fact.fact_id!r} gateway {gateway!r} is not a declared component",
                )
            if fact.mode is ArchitectureFactMode.FORBIDDEN and (gateway is not None or required_control is not None):
                raise _fail(
                    "SDAI-ARCH-SECURITY-002",
                    f"forbidden boundary rule {fact.fact_id!r} cannot weaken itself with gateway/control qualifiers",
                )
            rules.append(
                _BoundaryRule(
                    fact=fact,
                    from_zone=fact.source,
                    to_zone=fact.target,
                    evidence_kind=evidence_kind,
                    direction=direction,
                    protocol=_text_attribute(attributes, "protocol"),
                    access=_text_attribute(attributes, "access"),
                    gateway=gateway,
                    required_control=required_control,
                    allow_sensitive_data=_bool_attribute(attributes, "allowSensitiveData", default=False),
                )
            )
            continue

        raise _fail(
            "SDAI-ARCH-SECURITY-002",
            f"trust-boundary fact {fact.fact_id!r} has unsupported role {role!r}",
        )

    memberships: dict[str, _Membership] = {}
    for subject, values in sorted(memberships_by_subject.items()):
        zones = {item.zone for item in values}
        if len(zones) != 1:
            raise _fail(
                "SDAI-ARCH-SECURITY-003",
                f"trust zone ownership is ambiguous for {subject!r}: {', '.join(sorted(zones))}",
            )
        sensitive = {item.sensitive for item in values}
        exposures = {item.exposure for item in values}
        if len(sensitive) != 1 or len(exposures) != 1:
            raise _fail(
                "SDAI-ARCH-SECURITY-003",
                f"trust zone metadata is ambiguous for {subject!r}",
            )
        memberships[subject] = values[0]

    for rule in rules:
        if rule.gateway is None:
            continue
        membership = memberships.get(rule.gateway)
        if membership is None:
            raise _fail(
                "SDAI-ARCH-SECURITY-003",
                f"gateway {rule.gateway!r} requires an approved zone membership",
            )
        if membership.zone != rule.to_zone:
            raise _fail(
                "SDAI-ARCH-SECURITY-003",
                f"gateway {rule.gateway!r} must belong to boundary target zone {rule.to_zone!r}",
            )

    return memberships, tuple(sorted(rules, key=lambda item: item.fact.fact_id))


def _observations(values: Iterable[ArchitectureObservation]) -> tuple[ArchitectureObservation, ...]:
    observations = tuple(sorted(values, key=lambda item: (item.observer_id, item.sha256)))
    seen: set[str] = set()
    for observation in observations:
        if not isinstance(observation, ArchitectureObservation):
            raise _fail("SDAI-ARCH-SECURITY-001", "security derivation requires validated architecture observations")
        if observation.observer_id == TRUST_BOUNDARY_OBSERVER_ID:
            raise _fail("SDAI-ARCH-SECURITY-001", "input observations must not contain the derived trust-boundary observer")
        if observation.observer_id in seen:
            raise _fail("SDAI-ARCH-SECURITY-001", f"duplicate input observer {observation.observer_id!r}")
        seen.add(observation.observer_id)
    return observations


def _all_facts(observations: Iterable[ArchitectureObservation]) -> tuple[ObservedArchitectureFact, ...]:
    return tuple(fact for observation in observations for fact in observation.facts)


def _ownership_map(facts: Iterable[ObservedArchitectureFact]) -> dict[str, str]:
    owners: dict[str, set[str]] = {}
    for fact in facts:
        if fact.kind is ArchitectureFactKind.DATA_OWNERSHIP:
            owners.setdefault(fact.target, set()).add(fact.source)
    result: dict[str, str] = {}
    for target, values in sorted(owners.items()):
        if len(values) != 1:
            raise _fail(
                "SDAI-ARCH-SECURITY-003",
                f"data ownership is ambiguous for {target!r}: {', '.join(sorted(values))}",
            )
        result[target] = next(iter(values))
    return result


def _control_attestations(
    facts: Iterable[ObservedArchitectureFact],
) -> set[tuple[str, str, str | None]]:
    attestations: set[tuple[str, str, str | None]] = set()
    for fact in facts:
        if fact.kind is not ArchitectureFactKind.TRUST_BOUNDARY:
            continue
        attributes = _attributes(fact.attributes)
        if attributes.get("role") != "control-attestation":
            continue
        if set(attributes) - _ALLOWED_CONTROL_FIELDS:
            raise _fail("SDAI-ARCH-SECURITY-004", "control attestation contains unsupported attributes")
        target_subject = attributes.get("targetSubject")
        if target_subject is not None and (not isinstance(target_subject, str) or not target_subject):
            raise _fail("SDAI-ARCH-SECURITY-004", "control attestation targetSubject must be bounded text")
        attestations.add((fact.source, fact.target, target_subject if isinstance(target_subject, str) else None))
    return attestations


def _zone_for(
    subject: str,
    memberships: Mapping[str, _Membership],
    *,
    owners: Mapping[str, str],
) -> tuple[str | None, bool]:
    if subject == _EXTERNAL_SUBJECT or subject.startswith("external:") or subject == "endpoint:http":
        return _EXTERNAL_ZONE, False
    membership = memberships.get(subject)
    if membership is not None:
        return membership.zone, membership.sensitive
    owner = owners.get(subject)
    if owner is not None:
        owner_membership = memberships.get(owner)
        if owner_membership is not None:
            return owner_membership.zone, owner_membership.sensitive
    return None, False


def _crossing(
    *,
    source_subject: str,
    target_subject: str,
    from_zone: str,
    to_zone: str,
    evidence_kind: str,
    direction: str,
    protocol: str | None,
    access: str | None,
    sensitive_data: bool,
    provenance: Iterable[TraceProvenance],
) -> _Crossing:
    return _Crossing(
        source_subject=source_subject,
        target_subject=target_subject,
        from_zone=from_zone,
        to_zone=to_zone,
        evidence_kind=evidence_kind,
        direction=direction,
        protocol=protocol,
        access=access,
        sensitive_data=sensitive_data,
        provenance=_canonical_provenance(provenance),
    )


def _derive_crossings(
    approved: ApprovedArchitecture,
    observations: tuple[ArchitectureObservation, ...],
    memberships: Mapping[str, _Membership],
) -> tuple[tuple[_Crossing, ...], tuple[ArchitectureDriftFinding, ...]]:
    facts = _all_facts(observations)
    owners = _ownership_map(facts)
    crossings: list[_Crossing] = []
    resolution_findings: list[ArchitectureDriftFinding] = []

    def resolve(
        source_subject: str,
        target_subject: str,
        *,
        evidence_kind: str,
        direction: str,
        protocol: str | None,
        access: str | None,
        provenance: tuple[TraceProvenance, ...],
    ) -> None:
        from_zone, _ = _zone_for(source_subject, memberships, owners=owners)
        to_zone, sensitive = _zone_for(target_subject, memberships, owners=owners)
        missing = source_subject if from_zone is None else target_subject if to_zone is None else None
        if missing is not None:
            resolution_findings.append(
                ArchitectureDriftFinding(
                    code="ARCH-SEC-ZONE-MISSING",
                    severity=SECURITY_DRIFT_SEVERITY_CANDIDATES["ARCH-SEC-ZONE-MISSING"],
                    kind=ArchitectureFactKind.TRUST_BOUNDARY,
                    source=source_subject,
                    target=target_subject,
                    attributes={
                        "role": "zone-resolution",
                        "subject": missing,
                        "evidenceKind": evidence_kind,
                    },
                    approved_fact_id=None,
                    approved_provenance=_topology_provenance(
                        approved,
                        "approved topology does not assign a unique trust zone to the affected subject",
                    ),
                    observed_provenance=provenance,
                    message=f"security-sensitive architecture edge cannot resolve trust zone for {missing!r}",
                )
            )
            return
        assert from_zone is not None and to_zone is not None
        if from_zone == to_zone:
            return
        crossings.append(
            _crossing(
                source_subject=source_subject,
                target_subject=target_subject,
                from_zone=from_zone,
                to_zone=to_zone,
                evidence_kind=evidence_kind,
                direction=direction,
                protocol=protocol,
                access=access,
                sensitive_data=sensitive,
                provenance=provenance,
            )
        )

    component_ids = {component.component_id for component in approved.topology.components}
    for fact in facts:
        attributes = _attributes(fact.attributes)
        if fact.kind is ArchitectureFactKind.COMMUNICATION:
            direction = attributes.get("direction")
            protocol = attributes.get("protocol")
            if not isinstance(direction, str) or direction not in {"inbound", "outbound"}:
                raise _fail("SDAI-ARCH-SECURITY-004", "communication observation has invalid direction for security derivation")
            if not isinstance(protocol, str) or not protocol:
                raise _fail("SDAI-ARCH-SECURITY-004", "communication observation has invalid protocol for security derivation")
            if direction == "inbound" and fact.target == "endpoint:http":
                resolve(
                    _EXTERNAL_SUBJECT,
                    fact.source,
                    evidence_kind="communication",
                    direction="inbound",
                    protocol=protocol,
                    access=None,
                    provenance=fact.provenance,
                )
                continue
            target = fact.target
            if target not in component_ids and not target.startswith("external:"):
                # Other synthetic communication targets are not boundary subjects until explicitly modeled.
                continue
            resolve(
                fact.source,
                target,
                evidence_kind="communication",
                direction="outbound",
                protocol=protocol,
                access=None,
                provenance=fact.provenance,
            )
            continue

        if fact.kind is ArchitectureFactKind.DATA_ACCESS:
            access = attributes.get("access")
            if not isinstance(access, str) or not access:
                raise _fail("SDAI-ARCH-SECURITY-004", "data-access observation has invalid access mode for security derivation")
            resolve(
                fact.source,
                fact.target,
                evidence_kind="data-access",
                direction="outbound",
                protocol=None,
                access=access,
                provenance=fact.provenance,
            )
            continue

        if fact.kind is ArchitectureFactKind.DEPLOYMENT:
            direction = attributes.get("direction", "placement")
            if not isinstance(direction, str) or direction not in _ALLOWED_DIRECTIONS:
                raise _fail("SDAI-ARCH-SECURITY-004", "deployment observation has invalid direction for security derivation")
            resolve(
                fact.source,
                fact.target,
                evidence_kind="deployment",
                direction=direction,
                protocol=None,
                access=None,
                provenance=fact.provenance,
            )

    by_key: dict[tuple[object, ...], list[_Crossing]] = {}
    for item in crossings:
        key = (
            item.source_subject,
            item.target_subject,
            item.from_zone,
            item.to_zone,
            item.evidence_kind,
            item.direction,
            item.protocol,
            item.access,
            item.sensitive_data,
        )
        by_key.setdefault(key, []).append(item)
    merged: list[_Crossing] = []
    for key in sorted(by_key, key=lambda value: tuple("" if item is None else str(item) for item in value)):
        values = by_key[key]
        first = values[0]
        merged.append(
            _crossing(
                source_subject=first.source_subject,
                target_subject=first.target_subject,
                from_zone=first.from_zone,
                to_zone=first.to_zone,
                evidence_kind=first.evidence_kind,
                direction=first.direction,
                protocol=first.protocol,
                access=first.access,
                sensitive_data=first.sensitive_data,
                provenance=(provenance for value in values for provenance in value.provenance),
            )
        )
    return tuple(merged), tuple(resolution_findings)


def _dimension_match(rule: _BoundaryRule, crossing: _Crossing, *, include_direction: bool = True) -> bool:
    if rule.from_zone != crossing.from_zone or rule.to_zone != crossing.to_zone:
        return False
    if rule.evidence_kind != crossing.evidence_kind:
        return False
    if include_direction and rule.direction != crossing.direction:
        return False
    if rule.protocol is not None and rule.protocol != crossing.protocol:
        return False
    if rule.access is not None and rule.access != crossing.access:
        return False
    return True


def _rule_provenance(rule: _BoundaryRule) -> tuple[TraceProvenance, ...]:
    return rule.fact.provenance


def _security_finding(
    approved: ApprovedArchitecture,
    crossing: _Crossing,
    *,
    code: str,
    message: str,
    rule: _BoundaryRule | None = None,
    attributes: Mapping[str, object] | None = None,
) -> ArchitectureDriftFinding:
    return ArchitectureDriftFinding(
        code=code,
        severity=SECURITY_DRIFT_SEVERITY_CANDIDATES[code],
        kind=ArchitectureFactKind.TRUST_BOUNDARY,
        source=crossing.source_subject,
        target=crossing.target_subject,
        attributes=attributes
        or {
            "role": "security-crossing",
            "fromZone": crossing.from_zone,
            "toZone": crossing.to_zone,
            "evidenceKind": crossing.evidence_kind,
            "direction": crossing.direction,
        },
        approved_fact_id=rule.fact.fact_id if rule is not None else None,
        approved_provenance=_rule_provenance(rule)
        if rule is not None
        else _topology_provenance(approved, "approved topology has no matching trust-boundary rule"),
        observed_provenance=crossing.provenance,
        message=message,
    )


def _control_present(
    crossing: _Crossing,
    control: str,
    attestations: set[tuple[str, str, str | None]],
) -> bool:
    return (crossing.source_subject, control, crossing.target_subject) in attestations or (
        crossing.source_subject,
        control,
        None,
    ) in attestations


def _evaluate_crossings(
    approved: ApprovedArchitecture,
    crossings: tuple[_Crossing, ...],
    rules: tuple[_BoundaryRule, ...],
    attestations: set[tuple[str, str, str | None]],
) -> tuple[ArchitectureDriftFinding, ...]:
    findings: list[ArchitectureDriftFinding] = []

    for crossing in crossings:
        dimensional = [rule for rule in rules if _dimension_match(rule, crossing)]
        forbidden = [rule for rule in dimensional if rule.fact.mode is ArchitectureFactMode.FORBIDDEN]
        if forbidden:
            rule = forbidden[0]
            findings.append(
                _security_finding(
                    approved,
                    crossing,
                    code="ARCH-SEC-BOUNDARY-FORBIDDEN",
                    rule=rule,
                    message=f"observed {crossing.evidence_kind} edge crosses a forbidden trust boundary",
                )
            )
            continue

        permissive = [
            rule
            for rule in dimensional
            if rule.fact.mode in {ArchitectureFactMode.ALLOWED, ArchitectureFactMode.REQUIRED}
        ]
        if permissive:
            gateways = {rule.gateway for rule in permissive if rule.gateway is not None}
            if len(gateways) > 1:
                raise _fail(
                    "SDAI-ARCH-SECURITY-003",
                    f"matching trust-boundary rules require conflicting gateways: {', '.join(sorted(gateways))}",
                )
            gateway = next(iter(gateways), None)
            if gateway is not None and crossing.target_subject != gateway:
                strict_rule = next(rule for rule in permissive if rule.gateway == gateway)
                findings.append(
                    _security_finding(
                        approved,
                        crossing,
                        code="ARCH-SEC-GATEWAY-BYPASS",
                        rule=strict_rule,
                        attributes={
                            "role": "gateway-bypass",
                            "fromZone": crossing.from_zone,
                            "toZone": crossing.to_zone,
                            "requiredGateway": gateway,
                            "evidenceKind": crossing.evidence_kind,
                        },
                        message=f"trust-boundary crossing bypasses required gateway {gateway!r}",
                    )
                )
                continue

            if crossing.sensitive_data and not all(rule.allow_sensitive_data for rule in permissive):
                strict_rule = next(rule for rule in permissive if not rule.allow_sensitive_data)
                findings.append(
                    _security_finding(
                        approved,
                        crossing,
                        code="ARCH-SEC-SENSITIVE-DATA-CROSSING",
                        rule=strict_rule,
                        attributes={
                            "role": "sensitive-data-crossing",
                            "fromZone": crossing.from_zone,
                            "toZone": crossing.to_zone,
                            "access": crossing.access or "unknown",
                        },
                        message="sensitive data crosses a trust boundary without explicit sensitive-data authorization",
                    )
                )
                continue

            controls = sorted({rule.required_control for rule in permissive if rule.required_control is not None})
            missing_controls = [control for control in controls if not _control_present(crossing, control, attestations)]
            if missing_controls:
                strict_rule = next(rule for rule in permissive if rule.required_control == missing_controls[0])
                findings.append(
                    _security_finding(
                        approved,
                        crossing,
                        code="ARCH-SEC-CONTROL-MISSING",
                        rule=strict_rule,
                        attributes={
                            "role": "control-missing",
                            "fromZone": crossing.from_zone,
                            "toZone": crossing.to_zone,
                            "requiredControls": missing_controls,
                        },
                        message="trust-boundary crossing is missing required control attestation: " + ", ".join(missing_controls),
                    )
                )
            continue

        near_direction = [rule for rule in rules if _dimension_match(rule, crossing, include_direction=False)]
        if near_direction:
            rule = near_direction[0]
            findings.append(
                _security_finding(
                    approved,
                    crossing,
                    code="ARCH-SEC-DIRECTION-CHANGE",
                    rule=rule,
                    attributes={
                        "role": "direction-change",
                        "fromZone": crossing.from_zone,
                        "toZone": crossing.to_zone,
                        "approvedDirection": rule.direction,
                        "observedDirection": crossing.direction,
                    },
                    message="trust-boundary crossing direction differs from approved security topology",
                )
            )
            continue

        if _EXTERNAL_ZONE in {crossing.from_zone, crossing.to_zone}:
            findings.append(
                _security_finding(
                    approved,
                    crossing,
                    code="ARCH-SEC-EXPOSURE-CHANGE",
                    attributes={
                        "role": "exposure-change",
                        "fromZone": crossing.from_zone,
                        "toZone": crossing.to_zone,
                        "evidenceKind": crossing.evidence_kind,
                        "direction": crossing.direction,
                    },
                    message="repository observations introduce an undeclared external trust-boundary exposure",
                )
            )
            continue

        findings.append(
            _security_finding(
                approved,
                crossing,
                code="ARCH-SEC-BOUNDARY-UNAPPROVED",
                message="repository observations introduce a trust-boundary crossing not approved by security topology",
            )
        )

    for rule in rules:
        if rule.fact.mode is not ArchitectureFactMode.REQUIRED:
            continue
        matches = [crossing for crossing in crossings if _dimension_match(rule, crossing)]
        if rule.gateway is not None:
            matches = [crossing for crossing in matches if crossing.target_subject == rule.gateway]
        if matches:
            continue
        findings.append(
            ArchitectureDriftFinding(
                code="ARCH-SEC-REQUIRED-MISSING",
                severity=SECURITY_DRIFT_SEVERITY_CANDIDATES["ARCH-SEC-REQUIRED-MISSING"],
                kind=ArchitectureFactKind.TRUST_BOUNDARY,
                source=rule.from_zone,
                target=rule.to_zone,
                attributes={
                    "role": "required-boundary-missing",
                    "evidenceKind": rule.evidence_kind,
                    "direction": rule.direction,
                },
                approved_fact_id=rule.fact.fact_id,
                approved_provenance=rule.fact.provenance,
                observed_provenance=(),
                message=f"required trust-boundary relationship {rule.fact.fact_id!r} is not present in observations",
            )
        )

    return tuple(findings)


def derive_trust_boundary_observation(
    approved: ApprovedArchitecture,
    observations: Iterable[ArchitectureObservation],
) -> ArchitectureObservation:
    """Derive canonical cross-zone facts from existing provider-independent observations.

    Missing zone assignments are intentionally fail-closed here. Use
    ``evaluate_trust_boundary_security`` when policy-addressable missing-zone findings are desired.
    """
    if not isinstance(approved, ApprovedArchitecture):
        raise _fail("SDAI-ARCH-SECURITY-001", "security derivation requires validated approved architecture")
    canonical = _observations(observations)
    memberships, _ = _parse_security_topology(approved)
    crossings, resolution_findings = _derive_crossings(approved, canonical, memberships)
    if resolution_findings:
        subjects = sorted({str(item.attributes.get("subject")) for item in resolution_findings})
        raise _fail(
            "SDAI-ARCH-SECURITY-003",
            "trust-zone resolution is incomplete for: " + ", ".join(subjects),
        )
    return ArchitectureObservation(
        TRUST_BOUNDARY_OBSERVER_ID,
        tuple(crossing.observed_fact() for crossing in crossings),
    )


def evaluate_trust_boundary_security(
    approved: ApprovedArchitecture,
    observations: Iterable[ArchitectureObservation],
) -> ArchitectureDriftReport:
    """Return ordinary architecture drift plus deterministic security-topology findings."""
    if not isinstance(approved, ApprovedArchitecture):
        raise _fail("SDAI-ARCH-SECURITY-001", "security evaluation requires validated approved architecture")
    canonical = _observations(observations)
    memberships, rules = _parse_security_topology(approved)
    crossings, resolution_findings = _derive_crossings(approved, canonical, memberships)
    trust_observation = ArchitectureObservation(
        TRUST_BOUNDARY_OBSERVER_ID,
        tuple(crossing.observed_fact() for crossing in crossings),
    )
    base = compare_architecture(approved, (*canonical, trust_observation))
    facts = _all_facts(canonical)
    attestations = _control_attestations(facts)
    security_findings = _evaluate_crossings(approved, crossings, rules, attestations)
    non_security_findings = tuple(
        finding for finding in base.findings if finding.kind is not ArchitectureFactKind.TRUST_BOUNDARY
    )
    return ArchitectureDriftReport(
        topology_sha256=base.topology_sha256,
        approval_truth_sha256=base.approval_truth_sha256,
        observations=base.observations,
        findings=(*non_security_findings, *resolution_findings, *security_findings),
    )


__all__ = [
    "SECURITY_DRIFT_SEVERITY_CANDIDATES",
    "TRUST_BOUNDARY_OBSERVER_ID",
    "derive_trust_boundary_observation",
    "evaluate_trust_boundary_security",
]
