from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sdai.architecture_drift import (
    ArchitectureDriftError,
    ArchitectureFactKind,
    ArchitectureObservation,
    ObservedArchitectureFact,
    load_approved_architecture,
    load_architecture_topology,
)
from sdai.architecture_security_drift import (
    TRUST_BOUNDARY_OBSERVER_ID,
    derive_trust_boundary_observation,
    evaluate_trust_boundary_security,
)
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance


FEATURE = "ARCH-SEC-220"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        shell=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _fact(
    fact_id: str,
    *,
    kind: str,
    mode: str,
    source: str,
    target: str,
    attributes: dict[str, object],
) -> str:
    payload = json.dumps(attributes, sort_keys=True, separators=(",", ":"))
    return f"""    - id: {fact_id}
      kind: {kind}
      mode: {mode}
      source: {source}
      target: {target}
      attributes: {payload}
"""


def _membership(
    fact_id: str,
    subject: str,
    zone: str,
    *,
    sensitive: bool = False,
    exposure: str | None = None,
) -> str:
    attrs: dict[str, object] = {"role": "zone-membership"}
    if sensitive:
        attrs["sensitive"] = True
    if exposure is not None:
        attrs["exposure"] = exposure
    return _fact(
        fact_id,
        kind="trust-boundary",
        mode="allowed",
        source=subject,
        target=zone,
        attributes=attrs,
    )


def _rule(
    fact_id: str,
    from_zone: str,
    to_zone: str,
    *,
    mode: str = "allowed",
    evidence_kind: str = "communication",
    direction: str = "outbound",
    protocol: str | None = None,
    access: str | None = None,
    gateway: str | None = None,
    required_control: str | None = None,
    allow_sensitive_data: bool | None = None,
) -> str:
    attrs: dict[str, object] = {
        "role": "boundary-rule",
        "evidenceKind": evidence_kind,
        "direction": direction,
    }
    if protocol is not None:
        attrs["protocol"] = protocol
    if access is not None:
        attrs["access"] = access
    if gateway is not None:
        attrs["gateway"] = gateway
    if required_control is not None:
        attrs["requiredControl"] = required_control
    if allow_sensitive_data is not None:
        attrs["allowSensitiveData"] = allow_sensitive_data
    return _fact(
        fact_id,
        kind="trust-boundary",
        mode=mode,
        source=from_zone,
        target=to_zone,
        attributes=attrs,
    )


def _topology(root: Path, facts: str) -> None:
    approval = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    _write(
        root / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml",
        f"""apiVersion: sdai.architecture-topology/v1
kind: ApprovedArchitecture
metadata:
  id: security-topology
  feature: {FEATURE}
  approvalEvidence: {approval}
spec:
  components:
    - id: api
      roots: [src/api]
      modulePrefixes: [acme.api]
    - id: data
      roots: [src/data]
      modulePrefixes: [acme.data]
    - id: gateway
      roots: [src/gateway]
      modulePrefixes: [acme.gateway]
    - id: reporting
      roots: [src/reporting]
      modulePrefixes: [acme.reporting]
  facts:
{facts}""",
    )


def _approve(root: Path) -> None:
    topology = load_architecture_topology(root, FEATURE)
    evidence_relative = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    record = TraceEvidence(
        evidence_id="ARCH-APPROVAL-220",
        kind=EvidenceKind.APPROVAL,
        status=EvidenceStatus.PASSED,
        subject=topology.subject,
        git_commit=_git(root, "rev-parse", "HEAD"),
        bindings=(EvidenceBinding(EvidenceBindingKind.ARTIFACT, topology.source, topology.file_sha256),),
        provenance=(TraceProvenance(evidence_relative, 1, detail="security topology approval"),),
        producer=EvidenceProducer("architecture-approver", None, None),
        result={
            "architectureApproval": {
                "featureId": FEATURE,
                "topologyId": topology.topology_id,
                "topologySha256": topology.sha256,
            }
        },
        tool="sdai-architecture-approval",
    )
    _write(root / evidence_relative, record.to_json())


def _project(tmp_path: Path, facts: str) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "security-drift@example.invalid")
    _git(root, "config", "user.name", "Security Drift Tests")
    _topology(root, facts)
    for component in ("api", "data", "gateway", "reporting"):
        _write(root / "src" / component / "placeholder.txt", component + "\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "add approved security topology")
    _approve(root)
    return root


def _observed(
    observer: str,
    *,
    kind: ArchitectureFactKind,
    source: str,
    target: str,
    attributes: dict[str, object],
    provenance_source: str,
    line: int = 1,
) -> ArchitectureObservation:
    return ArchitectureObservation(
        observer,
        (
            ObservedArchitectureFact(
                kind=kind,
                source=source,
                target=target,
                attributes=attributes,
                provenance=(TraceProvenance(provenance_source, line, detail=f"{kind.value} observation"),),
            ),
        ),
    )


def _communication(
    *,
    source: str = "api",
    target: str = "data",
    direction: str = "outbound",
    protocol: str = "http",
    observer: str = "communications",
    provenance_source: str = "src/api/client.py",
) -> ArchitectureObservation:
    return _observed(
        observer,
        kind=ArchitectureFactKind.COMMUNICATION,
        source=source,
        target=target,
        attributes={"direction": direction, "protocol": protocol},
        provenance_source=provenance_source,
    )


def _approved_communication(
    fact_id: str,
    *,
    source: str = "api",
    target: str = "data",
    direction: str = "outbound",
    protocol: str = "http",
) -> str:
    return _fact(
        fact_id,
        kind="communication",
        mode="allowed",
        source=source,
        target=target,
        attributes={"direction": direction, "protocol": protocol},
    )


def _security_findings(report) -> list:
    return [item for item in report.findings if item.kind is ArchitectureFactKind.TRUST_BOUNDARY]


def test_allowed_cross_zone_communication_is_deterministic_and_clean(tmp_path: Path) -> None:
    facts = "".join(
        (
            _membership("ZONE-API", "api", "zone:public", exposure="public"),
            _membership("ZONE-DATA", "data", "zone:internal", exposure="internal"),
            _rule("PUBLIC-INTERNAL-HTTP", "zone:public", "zone:internal", protocol="http"),
            _approved_communication("COMM-API-DATA"),
        )
    )
    root = _project(tmp_path, facts)
    approved = load_approved_architecture(root, FEATURE)
    observation = _communication()

    first = evaluate_trust_boundary_security(approved, (observation,))
    second = evaluate_trust_boundary_security(approved, (observation,))
    derived = derive_trust_boundary_observation(approved, (observation,))

    assert first.to_json() == second.to_json()
    assert first.findings == ()
    assert derived.observer_id == TRUST_BOUNDARY_OBSERVER_ID
    assert len(derived.facts) == 1
    crossing = derived.facts[0]
    assert crossing.source == "api"
    assert crossing.target == "data"
    assert dict(crossing.attributes) == {
        "role": "crossing",
        "fromZone": "zone:public",
        "toZone": "zone:internal",
        "evidenceKind": "communication",
        "direction": "outbound",
        "protocol": "http",
    }
    assert crossing.provenance[0].source == "src/api/client.py"


def test_forbidden_crossing_is_security_error_with_approved_and_observed_provenance(tmp_path: Path) -> None:
    facts = "".join(
        (
            _membership("ZONE-API", "api", "zone:public"),
            _membership("ZONE-DATA", "data", "zone:privileged"),
            _rule(
                "FORBID-PUBLIC-PRIVILEGED",
                "zone:public",
                "zone:privileged",
                mode="forbidden",
                protocol="http",
            ),
            _approved_communication("COMM-API-DATA"),
        )
    )
    root = _project(tmp_path, facts)
    report = evaluate_trust_boundary_security(load_approved_architecture(root, FEATURE), (_communication(),))

    findings = _security_findings(report)
    assert [item.code for item in findings] == ["ARCH-SEC-BOUNDARY-FORBIDDEN"]
    finding = findings[0]
    assert finding.approved_fact_id == "FORBID-PUBLIC-PRIVILEGED"
    assert finding.approved_provenance[0].source.endswith("approved-topology.yaml")
    assert finding.observed_provenance[0].source == "src/api/client.py"


def test_required_gateway_allows_gateway_entry_and_rejects_direct_bypass(tmp_path: Path) -> None:
    base = "".join(
        (
            _membership("ZONE-API", "api", "zone:public"),
            _membership("ZONE-DATA", "data", "zone:internal"),
            _membership("ZONE-GATEWAY", "gateway", "zone:internal"),
            _rule(
                "PUBLIC-INTERNAL-VIA-GATEWAY",
                "zone:public",
                "zone:internal",
                protocol="http",
                gateway="gateway",
            ),
        )
    )

    direct_root = _project(tmp_path / "direct", base + _approved_communication("COMM-DIRECT"))
    direct_report = evaluate_trust_boundary_security(
        load_approved_architecture(direct_root, FEATURE),
        (_communication(),),
    )
    assert [item.code for item in _security_findings(direct_report)] == ["ARCH-SEC-GATEWAY-BYPASS"]

    gateway_facts = base + _approved_communication("COMM-GATEWAY", target="gateway")
    gateway_root = _project(tmp_path / "gateway", gateway_facts)
    gateway_report = evaluate_trust_boundary_security(
        load_approved_architecture(gateway_root, FEATURE),
        (_communication(target="gateway"),),
    )
    assert _security_findings(gateway_report) == []


def test_required_control_must_have_explicit_provider_independent_attestation(tmp_path: Path) -> None:
    facts = "".join(
        (
            _membership("ZONE-API", "api", "zone:public"),
            _membership("ZONE-DATA", "data", "zone:internal"),
            _rule(
                "CONTROLLED-HTTP",
                "zone:public",
                "zone:internal",
                protocol="http",
                required_control="control:mtls",
            ),
            _approved_communication("COMM-API-DATA"),
        )
    )
    root = _project(tmp_path, facts)
    approved = load_approved_architecture(root, FEATURE)
    communication = _communication()

    missing = evaluate_trust_boundary_security(approved, (communication,))
    assert [item.code for item in _security_findings(missing)] == ["ARCH-SEC-CONTROL-MISSING"]

    control = _observed(
        "security-controls",
        kind=ArchitectureFactKind.TRUST_BOUNDARY,
        source="api",
        target="control:mtls",
        attributes={"role": "control-attestation", "targetSubject": "data"},
        provenance_source="security/mtls.yaml",
    )
    allowed = evaluate_trust_boundary_security(approved, (communication, control))
    assert _security_findings(allowed) == []


def test_sensitive_data_crossing_requires_explicit_sensitive_data_authorization(tmp_path: Path) -> None:
    resource = "data:resource:customers"
    access_attrs = {"resource": "customers", "resourceType": "table", "access": "read"}
    facts = "".join(
        (
            _membership("ZONE-REPORT", "reporting", "zone:public"),
            _membership("ZONE-CUSTOMERS", resource, "zone:privileged", sensitive=True),
            _rule(
                "PUBLIC-PRIVILEGED-DATA",
                "zone:public",
                "zone:privileged",
                evidence_kind="data-access",
                access="read",
                allow_sensitive_data=False,
            ),
            _fact(
                "REPORT-READ-CUSTOMERS",
                kind="data-access",
                mode="allowed",
                source="reporting",
                target=resource,
                attributes=access_attrs,
            ),
        )
    )
    root = _project(tmp_path, facts)
    access = _observed(
        "data-access",
        kind=ArchitectureFactKind.DATA_ACCESS,
        source="reporting",
        target=resource,
        attributes=access_attrs,
        provenance_source="src/reporting/query.sql",
    )
    report = evaluate_trust_boundary_security(load_approved_architecture(root, FEATURE), (access,))

    assert [item.code for item in _security_findings(report)] == ["ARCH-SEC-SENSITIVE-DATA-CROSSING"]


def test_data_resource_zone_can_be_derived_from_unique_observed_owner(tmp_path: Path) -> None:
    resource = "data:resource:customers"
    ownership_attrs = {"resource": "customers", "resourceType": "table"}
    access_attrs = {"resource": "customers", "resourceType": "table", "access": "read"}
    facts = "".join(
        (
            _membership("ZONE-REPORT", "reporting", "zone:public"),
            _membership("ZONE-DATA", "data", "zone:internal"),
            _rule(
                "PUBLIC-INTERNAL-DATA",
                "zone:public",
                "zone:internal",
                evidence_kind="data-access",
                access="read",
                allow_sensitive_data=True,
            ),
            _fact(
                "DATA-OWNS-CUSTOMERS",
                kind="data-ownership",
                mode="allowed",
                source="data",
                target=resource,
                attributes=ownership_attrs,
            ),
            _fact(
                "REPORT-READ-CUSTOMERS",
                kind="data-access",
                mode="allowed",
                source="reporting",
                target=resource,
                attributes=access_attrs,
            ),
        )
    )
    root = _project(tmp_path, facts)
    ownership = _observed(
        "data-ownership",
        kind=ArchitectureFactKind.DATA_OWNERSHIP,
        source="data",
        target=resource,
        attributes=ownership_attrs,
        provenance_source="src/data/001.sql",
    )
    access = _observed(
        "data-access",
        kind=ArchitectureFactKind.DATA_ACCESS,
        source="reporting",
        target=resource,
        attributes=access_attrs,
        provenance_source="src/reporting/query.sql",
    )

    report = evaluate_trust_boundary_security(load_approved_architecture(root, FEATURE), (ownership, access))
    assert _security_findings(report) == []
    derived = next(item for item in report.observations if item.observer_id == TRUST_BOUNDARY_OBSERVER_ID)
    assert len(derived.facts) == 1
    assert dict(derived.facts[0].attributes)["toZone"] == "zone:internal"


def test_missing_zone_is_policy_addressable_and_derive_api_fails_closed(tmp_path: Path) -> None:
    facts = "".join(
        (
            _membership("ZONE-API", "api", "zone:public"),
            _approved_communication("COMM-API-DATA"),
        )
    )
    root = _project(tmp_path, facts)
    approved = load_approved_architecture(root, FEATURE)
    communication = _communication()

    report = evaluate_trust_boundary_security(approved, (communication,))
    findings = _security_findings(report)
    assert [item.code for item in findings] == ["ARCH-SEC-ZONE-MISSING"]
    assert dict(findings[0].attributes)["subject"] == "data"

    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-SECURITY-003.*data"):
        derive_trust_boundary_observation(approved, (communication,))


def test_ambiguous_zone_membership_fails_closed(tmp_path: Path) -> None:
    facts = "".join(
        (
            _membership("ZONE-API", "api", "zone:public"),
            _membership("ZONE-DATA-A", "data", "zone:internal"),
            _membership("ZONE-DATA-B", "data", "zone:privileged"),
            _approved_communication("COMM-API-DATA"),
        )
    )
    root = _project(tmp_path, facts)

    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-SECURITY-003.*ambiguous.*data"):
        evaluate_trust_boundary_security(load_approved_architecture(root, FEATURE), (_communication(),))


def test_inbound_external_direction_and_exposure_changes_are_classified_stably(tmp_path: Path) -> None:
    inbound_attrs = {"direction": "inbound", "protocol": "http"}
    facts = "".join(
        (
            _membership("ZONE-API", "api", "zone:internal"),
            _rule(
                "WRONG-DIRECTION",
                "zone:external",
                "zone:internal",
                direction="outbound",
                protocol="http",
            ),
            _fact(
                "HTTP-INBOUND",
                kind="communication",
                mode="allowed",
                source="api",
                target="endpoint:http",
                attributes=inbound_attrs,
            ),
        )
    )
    root = _project(tmp_path, facts)
    inbound = _observed(
        "communications",
        kind=ArchitectureFactKind.COMMUNICATION,
        source="api",
        target="endpoint:http",
        attributes=inbound_attrs,
        provenance_source="src/api/routes.py",
    )
    report = evaluate_trust_boundary_security(load_approved_architecture(root, FEATURE), (inbound,))
    assert [item.code for item in _security_findings(report)] == ["ARCH-SEC-DIRECTION-CHANGE"]

    no_rule_facts = "".join(
        (
            _membership("ZONE-API", "api", "zone:internal"),
            _fact(
                "HTTP-INBOUND",
                kind="communication",
                mode="allowed",
                source="api",
                target="endpoint:http",
                attributes=inbound_attrs,
            ),
        )
    )
    no_rule_root = _project(tmp_path / "no-rule", no_rule_facts)
    exposure = evaluate_trust_boundary_security(load_approved_architecture(no_rule_root, FEATURE), (inbound,))
    assert [item.code for item in _security_findings(exposure)] == ["ARCH-SEC-EXPOSURE-CHANGE"]


def test_required_boundary_rule_missing_is_error(tmp_path: Path) -> None:
    facts = "".join(
        (
            _membership("ZONE-API", "api", "zone:public"),
            _membership("ZONE-DATA", "data", "zone:internal"),
            _rule(
                "REQUIRED-PUBLIC-INTERNAL",
                "zone:public",
                "zone:internal",
                mode="required",
                protocol="http",
            ),
        )
    )
    root = _project(tmp_path, facts)
    report = evaluate_trust_boundary_security(load_approved_architecture(root, FEATURE), ())

    findings = _security_findings(report)
    assert [item.code for item in findings] == ["ARCH-SEC-REQUIRED-MISSING"]
    assert findings[0].approved_fact_id == "REQUIRED-PUBLIC-INTERNAL"


def test_deployment_crossing_is_supported_without_cloud_runtime_probing(tmp_path: Path) -> None:
    deployment_attrs = {"direction": "placement", "environment": "prod"}
    facts = "".join(
        (
            _membership("ZONE-API", "api", "zone:internal"),
            _membership("ZONE-WORKLOAD", "workload:prod", "zone:privileged"),
            _rule(
                "INTERNAL-PRIVILEGED-DEPLOY",
                "zone:internal",
                "zone:privileged",
                evidence_kind="deployment",
                direction="placement",
            ),
            _fact(
                "API-PROD-DEPLOY",
                kind="deployment",
                mode="allowed",
                source="api",
                target="workload:prod",
                attributes=deployment_attrs,
            ),
        )
    )
    root = _project(tmp_path, facts)
    deployment = _observed(
        "deployments",
        kind=ArchitectureFactKind.DEPLOYMENT,
        source="api",
        target="workload:prod",
        attributes=deployment_attrs,
        provenance_source="deploy/k8s.yaml",
    )

    report = evaluate_trust_boundary_security(load_approved_architecture(root, FEATURE), (deployment,))
    assert _security_findings(report) == []
    derived = next(item for item in report.observations if item.observer_id == TRUST_BOUNDARY_OBSERVER_ID)
    assert dict(derived.facts[0].attributes)["evidenceKind"] == "deployment"
