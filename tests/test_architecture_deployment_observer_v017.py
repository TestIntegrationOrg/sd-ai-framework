from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sdai.architecture_deployment_observer import (
    DEPLOYMENT_OBSERVER_ID,
    DeploymentTopologyObserver,
    deployment_subject_id,
    load_deployment_sources,
)
from sdai.architecture_drift import (
    ArchitectureDriftError,
    ArchitectureFactKind,
    compare_architecture,
    load_approved_architecture,
    load_architecture_topology,
)
from sdai.architecture_security_drift import evaluate_trust_boundary_security
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance

FEATURE = "ARCH-DEPLOY-221"


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


def _topology(root: Path, facts: str) -> None:
    approval = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    _write(root / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml", f"""apiVersion: sdai.architecture-topology/v1
kind: ApprovedArchitecture
metadata:
  id: deployment-topology
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
  facts:
{facts if facts else '    []'}
""")


def _approve(root: Path) -> None:
    topology = load_architecture_topology(root, FEATURE)
    relative = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    record = TraceEvidence(
        evidence_id="ARCH-APPROVAL-221", kind=EvidenceKind.APPROVAL,
        status=EvidenceStatus.PASSED, subject=topology.subject,
        git_commit=_git(root, "rev-parse", "HEAD"),
        bindings=(EvidenceBinding(EvidenceBindingKind.ARTIFACT, topology.source, topology.file_sha256),),
        provenance=(TraceProvenance(relative, 1, detail="deployment topology approval"),),
        producer=EvidenceProducer("architecture-approver", None, None),
        result={"architectureApproval": {
            "featureId": FEATURE, "topologyId": topology.topology_id,
            "topologySha256": topology.sha256,
        }}, tool="sdai-architecture-approval",
    )
    _write(root / relative, record.to_json())


def _project(base: Path, facts: str = "") -> Path:
    root = base / "project"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "deployment@example.invalid")
    _git(root, "config", "user.name", "Deployment Tests")
    _topology(root, facts)
    _write(root / "src" / "api" / "placeholder.txt", "api\n")
    _write(root / "src" / "data" / "placeholder.txt", "data\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "add deployment topology")
    _approve(root)
    return root


def _sources(root: Path, entries: str) -> None:
    _write(root / ".sdai" / "deployments.yaml", f"""apiVersion: sdai.deployment-sources/v1
kind: DeploymentSources
sources:
{entries}""")


def _source_entry(source_id: str, kind: str, path: str, environment: str) -> str:
    return f"""  - id: {source_id}
    kind: {kind}
    path: {path}
    environment: {environment}
"""


def _observe(root: Path):
    approved = load_approved_architecture(root, FEATURE)
    return approved, DeploymentTopologyObserver().observe(root, approved)


def test_missing_deployment_manifest_is_backward_compatible_empty_observation(tmp_path: Path) -> None:
    root = _project(tmp_path)
    approved, first = _observe(root)
    second = DeploymentTopologyObserver().observe(root, approved)

    assert first.observer_id == DEPLOYMENT_OBSERVER_ID
    assert first.facts == ()
    assert first.to_json() == second.to_json()
    assert load_deployment_sources(root) == ()


def test_kubernetes_workload_and_public_service_match_approved_topology_and_redact_secrets(tmp_path: Path) -> None:
    workload = deployment_subject_id("prod-k8s", "kubernetes", "apps", "Deployment", "api")
    service = deployment_subject_id("prod-k8s", "kubernetes", "apps", "Service", "api-public")
    workload_attrs = {
        "role": "workload", "platform": "kubernetes", "sourceId": "prod-k8s",
        "environment": "prod", "namespace": "apps", "workloadKind": "Deployment",
        "name": "api", "direction": "placement", "ports": [{"port": 8080, "protocol": "TCP"}],
    }
    exposure_attrs = {
        "role": "exposure", "platform": "kubernetes", "sourceId": "prod-k8s",
        "environment": "prod", "namespace": "apps", "resourceKind": "Service",
        "name": "api-public", "exposure": "public", "direction": "inbound",
        "ports": [{"port": 443, "protocol": "TCP", "targetPort": 8080}],
    }
    facts = _fact("API-WORKLOAD", "deployment", "required", "api", workload, workload_attrs)
    facts += _fact("API-EXPOSURE", "deployment", "required", "api", service, exposure_attrs)
    root = _project(tmp_path, facts)
    _sources(root, _source_entry("prod-k8s", "kubernetes", "deploy/prod.yaml", "prod"))
    _write(root / "deploy" / "prod.yaml", """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: apps
  labels:
    sdai.io/component: api
spec:
  template:
    metadata:
      labels:
        sdai.io/component: api
    spec:
      containers:
        - name: api
          image: example.invalid/api:1
          env:
            - name: PASSWORD
              value: SUPERSECRET
          ports:
            - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: api-public
  namespace: apps
  labels:
    sdai.io/component: api
spec:
  type: LoadBalancer
  ports:
    - port: 443
      targetPort: 8080
---
apiVersion: v1
kind: Secret
metadata:
  name: api-secret
stringData:
  password: ANOTHERSECRET
""")

    approved, observation = _observe(root)
    assert compare_architecture(approved, (observation,)).findings == ()
    assert len(observation.facts) == 2
    assert "SUPERSECRET" not in observation.to_json()
    assert "ANOTHERSECRET" not in observation.to_json()
    assert observation.to_json() == DeploymentTopologyObserver().observe(root, approved).to_json()


def test_kubernetes_exposure_change_is_missing_old_and_unexpected_new_fact(tmp_path: Path) -> None:
    service = deployment_subject_id("prod-k8s", "kubernetes", "apps", "Service", "api")
    approved_attrs = {
        "role": "exposure", "platform": "kubernetes", "sourceId": "prod-k8s",
        "environment": "prod", "namespace": "apps", "resourceKind": "Service", "name": "api",
        "exposure": "internal", "direction": "inbound", "ports": [{"port": 80, "protocol": "TCP"}],
    }
    facts = _fact("API-SERVICE", "deployment", "required", "api", service, approved_attrs)
    root = _project(tmp_path, facts)
    _sources(root, _source_entry("prod-k8s", "kubernetes", "deploy/service.yaml", "prod"))
    _write(root / "deploy" / "service.yaml", """apiVersion: v1
kind: Service
metadata:
  name: api
  namespace: apps
  labels: {sdai.io/component: api}
spec:
  type: LoadBalancer
  ports: [{port: 80}]
""")

    approved, observation = _observe(root)
    report = compare_architecture(approved, (observation,))
    findings = [item for item in report.findings if item.kind is ArchitectureFactKind.DEPLOYMENT]
    assert {item.code for item in findings} == {"ARCH-DRIFT-REQUIRED-MISSING", "ARCH-DRIFT-UNEXPECTED-PRESENT"}


def test_compose_workloads_public_port_dependency_and_secret_redaction(tmp_path: Path) -> None:
    namespace = "compose:local"
    api_target = deployment_subject_id("local", "compose", namespace, "service", "api")
    data_target = deployment_subject_id("local", "compose", namespace, "service", "data")
    api_workload = {
        "role": "workload", "platform": "compose", "sourceId": "local", "environment": "dev",
        "namespace": namespace, "workloadKind": "service", "name": "api", "direction": "placement",
        "ports": [{"port": 8080, "protocol": "TCP", "published": 8080}],
    }
    data_workload = {
        "role": "workload", "platform": "compose", "sourceId": "local", "environment": "dev",
        "namespace": namespace, "workloadKind": "service", "name": "data", "direction": "placement", "ports": [],
    }
    exposure = {
        "role": "exposure", "platform": "compose", "sourceId": "local", "environment": "dev",
        "namespace": namespace, "resourceKind": "service", "name": "api", "exposure": "public",
        "direction": "inbound", "ports": [{"port": 8080, "protocol": "TCP", "published": 8080}],
    }
    dependency = {
        "role": "service-dependency", "platform": "compose", "sourceId": "local",
        "environment": "dev", "dependency": "data", "direction": "outbound",
    }
    facts = _fact("API-W", "deployment", "required", "api", api_target, api_workload)
    facts += _fact("DATA-W", "deployment", "required", "data", data_target, data_workload)
    facts += _fact("API-X", "deployment", "required", "api", api_target, exposure)
    facts += _fact("API-DATA", "deployment", "required", "api", "data", dependency)
    root = _project(tmp_path, facts)
    _sources(root, _source_entry("local", "compose", "compose.yaml", "dev"))
    _write(root / "compose.yaml", """services:
  api:
    x-sdai-component: api
    environment:
      PASSWORD: COMPOSESECRET
    ports: ["8080:8080"]
    depends_on: [data]
  data:
    x-sdai-component: data
    environment:
      TOKEN: DATASECRET
""")

    approved, observation = _observe(root)
    assert compare_architecture(approved, (observation,)).findings == ()
    assert "COMPOSESECRET" not in observation.to_json()
    assert "DATASECRET" not in observation.to_json()


def test_terraform_literal_metadata_and_dependency_are_bounded_and_secret_safe(tmp_path: Path) -> None:
    api_target = deployment_subject_id("infra", "terraform", "apps", "aws_ecs_service", "api")
    data_target = deployment_subject_id("infra", "terraform", "data", "aws_db_instance", "data")
    api_workload = {
        "role": "workload", "platform": "terraform", "sourceId": "infra", "environment": "prod",
        "namespace": "apps", "workloadKind": "aws_ecs_service", "name": "api", "direction": "placement",
        "ports": [{"port": 443, "protocol": "HTTPS"}],
    }
    api_exposure = {
        "role": "exposure", "platform": "terraform", "sourceId": "infra", "environment": "prod",
        "namespace": "apps", "resourceKind": "aws_ecs_service", "name": "api", "exposure": "public",
        "direction": "inbound", "ports": [{"port": 443, "protocol": "HTTPS"}],
    }
    data_workload = {
        "role": "workload", "platform": "terraform", "sourceId": "infra", "environment": "prod",
        "namespace": "data", "workloadKind": "aws_db_instance", "name": "data", "direction": "placement", "ports": [],
    }
    dependency = {
        "role": "service-dependency", "platform": "terraform", "sourceId": "infra",
        "environment": "prod", "dependency": "aws_db_instance.data", "direction": "outbound",
    }
    facts = _fact("TF-API", "deployment", "required", "api", api_target, api_workload)
    facts += _fact("TF-API-X", "deployment", "required", "api", api_target, api_exposure)
    facts += _fact("TF-DATA", "deployment", "required", "data", data_target, data_workload)
    facts += _fact("TF-DEP", "deployment", "required", "api", "data", dependency)
    root = _project(tmp_path, facts)
    _sources(root, _source_entry("infra", "terraform", "infra/main.tf", "prod"))
    _write(root / "infra" / "main.tf", """resource "aws_db_instance" "data" {
  sdai_component = "data"
  sdai_namespace = "data"
  password = "TERRAFORMSECRET"
}
resource "aws_ecs_service" "api" {
  sdai_component = "api"
  sdai_namespace = "apps"
  sdai_exposure = "public"
  sdai_port = 443
  sdai_protocol = "HTTPS"
  secret_token = "NEVEREMIT"
  depends_on = [aws_db_instance.data]
}
""")

    approved, observation = _observe(root)
    assert compare_architecture(approved, (observation,)).findings == ()
    assert "TERRAFORMSECRET" not in observation.to_json()
    assert "NEVEREMIT" not in observation.to_json()


def test_forbidden_colocation_and_required_isolation_use_observed_namespace_placement(tmp_path: Path) -> None:
    namespace = "compose:local"
    api_target = deployment_subject_id("local", "compose", namespace, "service", "api")
    data_target = deployment_subject_id("local", "compose", namespace, "service", "data")
    api_attrs = {"role": "workload", "platform": "compose", "sourceId": "local", "environment": "dev", "namespace": namespace, "workloadKind": "service", "name": "api", "direction": "placement", "ports": []}
    data_attrs = {"role": "workload", "platform": "compose", "sourceId": "local", "environment": "dev", "namespace": namespace, "workloadKind": "service", "name": "data", "direction": "placement", "ports": []}
    constraint = {"role": "co-location", "scope": "namespace", "environment": "dev"}
    isolation = {"role": "isolation", "scope": "namespace", "environment": "dev"}
    facts = _fact("API-W", "deployment", "required", "api", api_target, api_attrs)
    facts += _fact("DATA-W", "deployment", "required", "data", data_target, data_attrs)
    facts += _fact("NO-COLOCATE", "deployment", "forbidden", "api", "data", constraint)
    facts += _fact("REQUIRE-ISOLATION", "deployment", "required", "api", "data", isolation)
    root = _project(tmp_path, facts)
    _sources(root, _source_entry("local", "compose", "compose.yaml", "dev"))
    _write(root / "compose.yaml", """services:
  api: {x-sdai-component: api}
  data: {x-sdai-component: data}
""")

    approved, observation = _observe(root)
    report = compare_architecture(approved, (observation,))
    assert any(item.code == "ARCH-DRIFT-FORBIDDEN-PRESENT" and item.approved_fact_id == "NO-COLOCATE" for item in report.findings)
    assert any(item.code == "ARCH-DRIFT-REQUIRED-MISSING" and item.approved_fact_id == "REQUIRE-ISOLATION" for item in report.findings)


def test_ambiguous_path_and_explicit_component_mapping_fails_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _sources(root, _source_entry("api-k8s", "kubernetes", "src/api/deploy.yaml", "prod"))
    _write(root / "src" / "api" / "deploy.yaml", """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  labels: {sdai.io/component: data}
spec: {template: {metadata: {labels: {sdai.io/component: data}}, spec: {containers: []}}}
""")

    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DEPLOY-004.*ambiguous"):
        _observe(root)


def test_manifest_rejects_unsafe_paths_and_duplicate_keys(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _sources(root, _source_entry("bad", "kubernetes", "../outside.yaml", "prod"))
    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DEPLOY-002.*unsafe"):
        load_deployment_sources(root)

    _write(root / ".sdai" / "deployments.yaml", """apiVersion: sdai.deployment-sources/v1
kind: DeploymentSources
sources:
  - id: duplicate
    id: duplicate-again
    kind: compose
    path: compose.yaml
    environment: dev
""")
    with pytest.raises(ArchitectureDriftError, match="unique-key YAML"):
        load_deployment_sources(root)


def test_workload_placement_composes_with_trust_boundary_engine(tmp_path: Path) -> None:
    target = deployment_subject_id("prod-k8s", "kubernetes", "prod-ns", "Deployment", "api")
    workload_attrs = {
        "role": "workload", "platform": "kubernetes", "sourceId": "prod-k8s", "environment": "prod",
        "namespace": "prod-ns", "workloadKind": "Deployment", "name": "api", "direction": "placement", "ports": [],
    }
    facts = _fact("API-W", "deployment", "allowed", "api", target, workload_attrs)
    facts += _fact("ZONE-API", "trust-boundary", "allowed", "api", "zone:internal", {"role": "zone-membership"})
    facts += _fact("ZONE-WORKLOAD", "trust-boundary", "allowed", target, "zone:privileged", {"role": "zone-membership"})
    facts += _fact("DEPLOY-BOUNDARY", "trust-boundary", "allowed", "zone:internal", "zone:privileged", {"role": "boundary-rule", "evidenceKind": "deployment", "direction": "placement"})
    root = _project(tmp_path, facts)
    _sources(root, _source_entry("prod-k8s", "kubernetes", "deploy/api.yaml", "prod"))
    _write(root / "deploy" / "api.yaml", """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: prod-ns
  labels: {sdai.io/component: api}
spec: {template: {metadata: {labels: {sdai.io/component: api}}, spec: {containers: []}}}
""")

    approved, deployment = _observe(root)
    security = evaluate_trust_boundary_security(approved, (deployment,))
    security_findings = [item for item in security.findings if item.kind is ArchitectureFactKind.TRUST_BOUNDARY]
    assert security_findings == []
