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
        ["git", *args], cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="strict", check=False, shell=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _fact(fid: str, kind: str, mode: str, source: str, target: str, attrs: dict[str, object]) -> str:
    payload = json.dumps(attrs, sort_keys=True, separators=(",", ":"))
    return f"""    - id: {fid}
      kind: {kind}
      mode: {mode}
      source: {source}
      target: {target}
      attributes: {payload}
"""


def _member(fid: str, subject: str, zone: str, *, sensitive: bool = False) -> str:
    attrs: dict[str, object] = {"role": "zone-membership"}
    if sensitive:
        attrs["sensitive"] = True
    return _fact(fid, "trust-boundary", "allowed", subject, zone, attrs)


def _rule(
    fid: str, from_zone: str, to_zone: str, *, mode: str = "allowed",
    evidence: str = "communication", direction: str = "outbound",
    protocol: str | None = None, access: str | None = None,
    gateway: str | None = None, control: str | None = None,
    allow_sensitive: bool | None = None,
) -> str:
    attrs: dict[str, object] = {
        "role": "boundary-rule", "evidenceKind": evidence, "direction": direction,
    }
    if protocol is not None:
        attrs["protocol"] = protocol
    if access is not None:
        attrs["access"] = access
    if gateway is not None:
        attrs["gateway"] = gateway
    if control is not None:
        attrs["requiredControl"] = control
    if allow_sensitive is not None:
        attrs["allowSensitiveData"] = allow_sensitive
    return _fact(fid, "trust-boundary", mode, from_zone, to_zone, attrs)


def _topology(root: Path, facts: str) -> None:
    approval = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    _write(root / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml", f"""apiVersion: sdai.architecture-topology/v1
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
{facts}""")


def _approve(root: Path) -> None:
    topology = load_architecture_topology(root, FEATURE)
    relative = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    record = TraceEvidence(
        evidence_id="ARCH-APPROVAL-220", kind=EvidenceKind.APPROVAL,
        status=EvidenceStatus.PASSED, subject=topology.subject,
        git_commit=_git(root, "rev-parse", "HEAD"),
        bindings=(EvidenceBinding(EvidenceBindingKind.ARTIFACT, topology.source, topology.file_sha256),),
        provenance=(TraceProvenance(relative, 1, detail="security topology approval"),),
        producer=EvidenceProducer("architecture-approver", None, None),
        result={"architectureApproval": {
            "featureId": FEATURE, "topologyId": topology.topology_id,
            "topologySha256": topology.sha256,
        }}, tool="sdai-architecture-approval",
    )
    _write(root / relative, record.to_json())


def _project(base: Path, facts: str) -> Path:
    root = base / "project"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "security@example.invalid")
    _git(root, "config", "user.name", "Security Tests")
    _topology(root, facts)
    for name in ("api", "data", "gateway", "reporting"):
        _write(root / "src" / name / "placeholder.txt", name + "\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "add security topology")
    _approve(root)
    return root


def _obs(
    observer: str, kind: ArchitectureFactKind, source: str, target: str,
    attrs: dict[str, object], source_file: str,
) -> ArchitectureObservation:
    return ArchitectureObservation(observer, (
        ObservedArchitectureFact(
            kind=kind, source=source, target=target, attributes=attrs,
            provenance=(TraceProvenance(source_file, 1, detail=f"{kind.value} observation"),),
        ),
    ))


def _comm(*, target: str = "data", direction: str = "outbound") -> ArchitectureObservation:
    return _obs(
        "communications", ArchitectureFactKind.COMMUNICATION, "api", target,
        {"direction": direction, "protocol": "http"}, "src/api/client.py",
    )


def _approved_comm(fid: str, *, target: str = "data", direction: str = "outbound") -> str:
    return _fact(fid, "communication", "allowed", "api", target, {"direction": direction, "protocol": "http"})


def _sec(report) -> list:
    return [item for item in report.findings if item.kind is ArchitectureFactKind.TRUST_BOUNDARY]


def test_allowed_crossing_is_clean_and_deterministic(tmp_path: Path) -> None:
    facts = _member("ZONE-API", "api", "zone:public") + _member("ZONE-DATA", "data", "zone:internal")
    facts += _rule("ALLOW-HTTP", "zone:public", "zone:internal", protocol="http")
    facts += _approved_comm("COMM")
    root = _project(tmp_path, facts)
    approved = load_approved_architecture(root, FEATURE)
    observation = _comm()

    first = evaluate_trust_boundary_security(approved, (observation,))
    second = evaluate_trust_boundary_security(approved, (observation,))
    derived = derive_trust_boundary_observation(approved, (observation,))

    assert first.to_json() == second.to_json()
    assert first.findings == ()
    assert derived.observer_id == TRUST_BOUNDARY_OBSERVER_ID
    assert len(derived.facts) == 1
    assert dict(derived.facts[0].attributes) == {
        "role": "crossing", "fromZone": "zone:public", "toZone": "zone:internal",
        "evidenceKind": "communication", "direction": "outbound", "protocol": "http",
    }


def test_forbidden_boundary_and_gateway_bypass_are_errors(tmp_path: Path) -> None:
    forbidden = _member("ZONE-API", "api", "zone:public") + _member("ZONE-DATA", "data", "zone:privileged")
    forbidden += _rule("FORBID", "zone:public", "zone:privileged", mode="forbidden", protocol="http")
    forbidden += _approved_comm("COMM")
    root = _project(tmp_path / "forbidden", forbidden)
    report = evaluate_trust_boundary_security(load_approved_architecture(root, FEATURE), (_comm(),))
    assert [item.code for item in _sec(report)] == ["ARCH-SEC-BOUNDARY-FORBIDDEN"]
    assert _sec(report)[0].observed_provenance[0].source == "src/api/client.py"

    gateway = _member("ZONE-API", "api", "zone:public") + _member("ZONE-DATA", "data", "zone:internal")
    gateway += _member("ZONE-GW", "gateway", "zone:internal")
    gateway += _rule("VIA-GW", "zone:public", "zone:internal", protocol="http", gateway="gateway")
    gateway += _approved_comm("DIRECT")
    direct_root = _project(tmp_path / "gateway", gateway)
    direct = evaluate_trust_boundary_security(load_approved_architecture(direct_root, FEATURE), (_comm(),))
    assert [item.code for item in _sec(direct)] == ["ARCH-SEC-GATEWAY-BYPASS"]


def test_gateway_entry_and_control_attestation_are_enforced(tmp_path: Path) -> None:
    facts = _member("ZONE-API", "api", "zone:public") + _member("ZONE-GW", "gateway", "zone:internal")
    facts += _rule("CONTROLLED", "zone:public", "zone:internal", protocol="http", gateway="gateway", control="control:mtls")
    facts += _approved_comm("COMM-GW", target="gateway")
    root = _project(tmp_path, facts)
    approved = load_approved_architecture(root, FEATURE)
    edge = _comm(target="gateway")

    missing = evaluate_trust_boundary_security(approved, (edge,))
    assert [item.code for item in _sec(missing)] == ["ARCH-SEC-CONTROL-MISSING"]

    control = _obs(
        "controls", ArchitectureFactKind.TRUST_BOUNDARY, "api", "control:mtls",
        {"role": "control-attestation", "targetSubject": "gateway"}, "security/mtls.yaml",
    )
    allowed = evaluate_trust_boundary_security(approved, (edge, control))
    assert _sec(allowed) == []


def test_sensitive_data_crossing_and_owner_derived_zone(tmp_path: Path) -> None:
    resource = "data:resource:customers"
    access_attrs = {"resource": "customers", "resourceType": "table", "access": "read"}
    sensitive = _member("ZONE-REPORT", "reporting", "zone:public") + _member("ZONE-RESOURCE", resource, "zone:privileged", sensitive=True)
    sensitive += _rule("DATA-RULE", "zone:public", "zone:privileged", evidence="data-access", access="read")
    sensitive += _fact("READ", "data-access", "allowed", "reporting", resource, access_attrs)
    root = _project(tmp_path / "sensitive", sensitive)
    access = _obs("data-access", ArchitectureFactKind.DATA_ACCESS, "reporting", resource, access_attrs, "src/reporting/query.sql")
    report = evaluate_trust_boundary_security(load_approved_architecture(root, FEATURE), (access,))
    assert [item.code for item in _sec(report)] == ["ARCH-SEC-SENSITIVE-DATA-CROSSING"]

    ownership_attrs = {"resource": "customers", "resourceType": "table"}
    derived = _member("ZONE-REPORT", "reporting", "zone:public") + _member("ZONE-DATA", "data", "zone:internal")
    derived += _rule("DATA-ALLOW", "zone:public", "zone:internal", evidence="data-access", access="read", allow_sensitive=True)
    derived += _fact("OWNER", "data-ownership", "allowed", "data", resource, ownership_attrs)
    derived += _fact("READ", "data-access", "allowed", "reporting", resource, access_attrs)
    owner_root = _project(tmp_path / "owner", derived)
    owner = _obs("ownership", ArchitectureFactKind.DATA_OWNERSHIP, "data", resource, ownership_attrs, "src/data/schema.sql")
    owner_report = evaluate_trust_boundary_security(load_approved_architecture(owner_root, FEATURE), (owner, access))
    assert _sec(owner_report) == []


def test_missing_and_ambiguous_zones_fail_closed_or_are_policy_addressable(tmp_path: Path) -> None:
    missing_facts = _member("ZONE-API", "api", "zone:public") + _approved_comm("COMM")
    root = _project(tmp_path / "missing", missing_facts)
    approved = load_approved_architecture(root, FEATURE)
    report = evaluate_trust_boundary_security(approved, (_comm(),))
    assert [item.code for item in _sec(report)] == ["ARCH-SEC-ZONE-MISSING"]
    assert dict(_sec(report)[0].attributes)["subject"] == "data"
    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-SECURITY-003.*data"):
        derive_trust_boundary_observation(approved, (_comm(),))

    ambiguous = _member("ZONE-API", "api", "zone:public")
    ambiguous += _member("ZONE-DATA-A", "data", "zone:internal") + _member("ZONE-DATA-B", "data", "zone:privileged")
    ambiguous += _approved_comm("COMM")
    ambiguous_root = _project(tmp_path / "ambiguous", ambiguous)
    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-SECURITY-003.*ambiguous.*data"):
        evaluate_trust_boundary_security(load_approved_architecture(ambiguous_root, FEATURE), (_comm(),))


def test_external_exposure_direction_and_required_boundary_are_classified(tmp_path: Path) -> None:
    inbound_attrs = {"direction": "inbound", "protocol": "http"}
    direction_facts = _member("ZONE-API", "api", "zone:internal")
    direction_facts += _rule("WRONG-DIR", "zone:external", "zone:internal", direction="outbound", protocol="http")
    direction_facts += _fact("INBOUND", "communication", "allowed", "api", "endpoint:http", inbound_attrs)
    root = _project(tmp_path / "direction", direction_facts)
    inbound = _obs("inbound", ArchitectureFactKind.COMMUNICATION, "api", "endpoint:http", inbound_attrs, "src/api/routes.py")
    report = evaluate_trust_boundary_security(load_approved_architecture(root, FEATURE), (inbound,))
    assert [item.code for item in _sec(report)] == ["ARCH-SEC-DIRECTION-CHANGE"]

    exposure_facts = _member("ZONE-API", "api", "zone:internal")
    exposure_facts += _fact("INBOUND", "communication", "allowed", "api", "endpoint:http", inbound_attrs)
    exposure_root = _project(tmp_path / "exposure", exposure_facts)
    exposure = evaluate_trust_boundary_security(load_approved_architecture(exposure_root, FEATURE), (inbound,))
    assert [item.code for item in _sec(exposure)] == ["ARCH-SEC-EXPOSURE-CHANGE"]

    required_facts = _member("ZONE-API", "api", "zone:public") + _member("ZONE-DATA", "data", "zone:internal")
    required_facts += _rule("REQUIRED", "zone:public", "zone:internal", mode="required", protocol="http")
    required_root = _project(tmp_path / "required", required_facts)
    required = evaluate_trust_boundary_security(load_approved_architecture(required_root, FEATURE), ())
    assert [item.code for item in _sec(required)] == ["ARCH-SEC-REQUIRED-MISSING"]


def test_deployment_crossing_is_supported_without_live_cloud_probing(tmp_path: Path) -> None:
    attrs = {"direction": "placement", "environment": "prod"}
    facts = _member("ZONE-API", "api", "zone:internal") + _member("ZONE-WORKLOAD", "workload:prod", "zone:privileged")
    facts += _rule("DEPLOY", "zone:internal", "zone:privileged", evidence="deployment", direction="placement")
    facts += _fact("DEPLOYMENT", "deployment", "allowed", "api", "workload:prod", attrs)
    root = _project(tmp_path, facts)
    observation = _obs("deployments", ArchitectureFactKind.DEPLOYMENT, "api", "workload:prod", attrs, "deploy/k8s.yaml")

    report = evaluate_trust_boundary_security(load_approved_architecture(root, FEATURE), (observation,))
    assert _sec(report) == []
    derived = next(item for item in report.observations if item.observer_id == TRUST_BOUNDARY_OBSERVER_ID)
    assert dict(derived.facts[0].attributes)["evidenceKind"] == "deployment"
