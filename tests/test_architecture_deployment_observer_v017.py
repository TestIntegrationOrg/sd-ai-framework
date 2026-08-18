from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sdai.architecture_deployment_observer import (
    DEPLOYMENT_OBSERVER_ID,
    DeploymentTopologyObserver,
    _strip_hcl_comments,
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
    facts_yaml = facts if facts else "    []\n"
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
{facts_yaml}""")


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


def _source(source_id: str, kind: str, path: str, environment: str) -> str:
    return f"""  - id: {source_id}
    kind: {kind}
    path: {path}
    environment: {environment}
"""


def _observe(root: Path):
    approved = load_approved_architecture(root, FEATURE)
    return approved, DeploymentTopologyObserver().observe(root, approved)


def test_missing_manifest_is_backward_compatible_and_deterministic(tmp_path: Path) -> None:
    root = _project(tmp_path)
    approved, first = _observe(root)
    assert first.observer_id == DEPLOYMENT_OBSERVER_ID
    assert first.facts == ()
    assert first.to_json() == DeploymentTopologyObserver().observe(root, approved).to_json()
    assert load_deployment_sources(root) == ()


def test_kubernetes_workload_public_exposure_and_secrets(tmp_path: Path) -> None:
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
        "ports": [{"port": 443, "protocol": "TCP", "targetPort": 8080}], "resource": service,
    }
    facts = _fact("API-WORKLOAD", "deployment", "required", "api", workload, workload_attrs)
    facts += _fact("API-EXPOSURE", "deployment", "required", "external:public", "api", exposure_attrs)
    root = _project(tmp_path, facts)
    _sources(root, _source("prod-k8s", "kubernetes", "deploy/prod.yaml", "prod"))
    _write(root / "deploy" / "prod.yaml", """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: apps
  labels: {sdai.io/component: api}
spec:
  template:
    metadata: {labels: {sdai.io/component: api}}
    spec:
      containers:
        - name: api
          image: example.invalid/api:1
          env: [{name: PASSWORD, value: SUPERSECRET}]
          ports: [{containerPort: 8080}]
---
apiVersion: v1
kind: Service
metadata:
  name: api-public
  namespace: apps
  labels: {sdai.io/component: api}
spec:
  type: LoadBalancer
  ports: [{port: 443, targetPort: 8080}]
---
apiVersion: v1
kind: Secret
metadata: {name: api-secret}
stringData: {password: ANOTHERSECRET}
""")

    approved, observation = _observe(root)
    assert compare_architecture(approved, (observation,)).findings == ()
    assert len(observation.facts) == 2
    assert "SUPERSECRET" not in observation.to_json()
    assert "ANOTHERSECRET" not in observation.to_json()
    assert observation.to_json() == DeploymentTopologyObserver().observe(root, approved).to_json()


def test_kubernetes_public_exposure_change_is_missing_and_unexpected(tmp_path: Path) -> None:
    service = deployment_subject_id("prod-k8s", "kubernetes", "apps", "Service", "api")
    internal = {
        "role": "exposure", "platform": "kubernetes", "sourceId": "prod-k8s",
        "environment": "prod", "namespace": "apps", "resourceKind": "Service", "name": "api",
        "exposure": "internal", "direction": "inbound", "ports": [{"port": 80, "protocol": "TCP"}],
        "resource": service,
    }
    root = _project(tmp_path, _fact("API-SVC", "deployment", "required", "api", "api", internal))
    _sources(root, _source("prod-k8s", "kubernetes", "deploy/service.yaml", "prod"))
    _write(root / "deploy" / "service.yaml", """apiVersion: v1
kind: Service
metadata: {name: api, namespace: apps, labels: {sdai.io/component: api}}
spec: {type: LoadBalancer, ports: [{port: 80}]}
""")

    approved, observation = _observe(root)
    report = compare_architecture(approved, (observation,))
    findings = [item for item in report.findings if item.kind is ArchitectureFactKind.DEPLOYMENT]
    assert {item.code for item in findings} == {"ARCH-DRIFT-REQUIRED-MISSING", "ARCH-DRIFT-UNEXPECTED-PRESENT"}
    public = next(item for item in observation.facts if dict(item.attributes).get("role") == "exposure")
    assert public.source == "external:public"
    assert public.target == "api"


def test_ingress_backend_resolution_is_document_order_independent(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _sources(root, _source("k8s", "kubernetes", "deploy/ingress.yaml", "prod"))
    _write(root / "deploy" / "ingress.yaml", """apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: public, namespace: apps}
spec:
  rules:
    - http:
        paths:
          - path: /
            pathType: Prefix
            backend: {service: {name: api-svc, port: {number: 80}}}
---
apiVersion: v1
kind: Service
metadata: {name: api-svc, namespace: apps, labels: {sdai.io/component: api}}
spec: {ports: [{port: 80}]}
""")
    _, observation = _observe(root)
    ingress = next(item for item in observation.facts if dict(item.attributes).get("resourceKind") == "Ingress")
    assert ingress.source == "external:public"
    assert ingress.target == "api"


def test_ingress_with_any_unresolved_backend_fails_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _sources(root, _source("k8s", "kubernetes", "deploy/ingress.yaml", "prod"))
    _write(root / "deploy" / "ingress.yaml", """apiVersion: v1
kind: Service
metadata: {name: api-svc, namespace: apps, labels: {sdai.io/component: api}}
spec: {ports: [{port: 80}]}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: public, namespace: apps}
spec:
  rules:
    - http:
        paths:
          - path: /ok
            pathType: Prefix
            backend: {service: {name: api-svc, port: {number: 80}}}
          - path: /missing
            pathType: Prefix
            backend: {service: {name: missing-svc, port: {number: 80}}}
""")
    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DEPLOY-004.*unresolved"):
        _observe(root)


def test_compose_workloads_dependency_public_port_and_secret_redaction(tmp_path: Path) -> None:
    namespace = "compose:local"
    api_target = deployment_subject_id("local", "compose", namespace, "service", "api")
    data_target = deployment_subject_id("local", "compose", namespace, "service", "data")
    api_w = {"role": "workload", "platform": "compose", "sourceId": "local", "environment": "dev", "namespace": namespace, "workloadKind": "service", "name": "api", "direction": "placement", "ports": [{"port": 8080, "protocol": "TCP", "published": 8080}]}
    data_w = {"role": "workload", "platform": "compose", "sourceId": "local", "environment": "dev", "namespace": namespace, "workloadKind": "service", "name": "data", "direction": "placement", "ports": []}
    exposure = {"role": "exposure", "platform": "compose", "sourceId": "local", "environment": "dev", "namespace": namespace, "resourceKind": "service", "name": "api", "exposure": "public", "direction": "inbound", "ports": [{"port": 8080, "protocol": "TCP", "published": 8080}], "resource": api_target}
    dependency = {"role": "service-dependency", "platform": "compose", "sourceId": "local", "environment": "dev", "dependency": "data", "direction": "outbound"}
    facts = _fact("API-W", "deployment", "required", "api", api_target, api_w)
    facts += _fact("DATA-W", "deployment", "required", "data", data_target, data_w)
    facts += _fact("API-X", "deployment", "required", "external:public", "api", exposure)
    facts += _fact("API-DATA", "deployment", "required", "api", "data", dependency)
    root = _project(tmp_path, facts)
    _sources(root, _source("local", "compose", "compose.yaml", "dev"))
    _write(root / "compose.yaml", """services:
  api:
    x-sdai-component: api
    environment: {PASSWORD: COMPOSESECRET}
    ports: ["8080:8080"]
    depends_on: [data]
  data:
    x-sdai-component: data
    environment: {TOKEN: DATASECRET}
""")
    approved, observation = _observe(root)
    assert compare_architecture(approved, (observation,)).findings == ()
    assert "COMPOSESECRET" not in observation.to_json()
    assert "DATASECRET" not in observation.to_json()


def test_terraform_literal_metadata_dependency_hcl_strings_and_secret_safety(tmp_path: Path) -> None:
    api_target = deployment_subject_id("infra", "terraform", "apps", "aws_ecs_service", "api")
    data_target = deployment_subject_id("infra", "terraform", "data", "aws_db_instance", "data")
    api_w = {"role": "workload", "platform": "terraform", "sourceId": "infra", "environment": "prod", "namespace": "apps", "workloadKind": "aws_ecs_service", "name": "api", "direction": "placement", "ports": [{"port": 443, "protocol": "HTTPS"}]}
    data_w = {"role": "workload", "platform": "terraform", "sourceId": "infra", "environment": "prod", "namespace": "data", "workloadKind": "aws_db_instance", "name": "data", "direction": "placement", "ports": []}
    exposure = {"role": "exposure", "platform": "terraform", "sourceId": "infra", "environment": "prod", "namespace": "apps", "resourceKind": "aws_ecs_service", "name": "api", "exposure": "public", "direction": "inbound", "ports": [{"port": 443, "protocol": "HTTPS"}], "resource": api_target}
    dependency = {"role": "service-dependency", "platform": "terraform", "sourceId": "infra", "environment": "prod", "dependency": "aws_db_instance.data", "direction": "outbound"}
    facts = _fact("TF-API", "deployment", "required", "api", api_target, api_w)
    facts += _fact("TF-DATA", "deployment", "required", "data", data_target, data_w)
    facts += _fact("TF-X", "deployment", "required", "external:public", "api", exposure)
    facts += _fact("TF-DEP", "deployment", "required", "api", "data", dependency)
    root = _project(tmp_path, facts)
    _sources(root, _source("infra", "terraform", "infra/main.tf", "prod"))
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
  callback_url = "https://example.invalid/a#fragment"
  secret_token = "NEVEREMIT//still-string"
  depends_on = [aws_db_instance.data]
}
""")
    approved, observation = _observe(root)
    assert compare_architecture(approved, (observation,)).findings == ()
    assert "TERRAFORMSECRET" not in observation.to_json()
    assert "NEVEREMIT" not in observation.to_json()
    cleaned = _strip_hcl_comments('value = "https://example.invalid/a#b//c" # comment\n')
    assert '"https://example.invalid/a#b//c"' in cleaned


def test_forbidden_colocation_and_required_isolation_use_observed_namespace(tmp_path: Path) -> None:
    ns = "compose:local"
    api_target = deployment_subject_id("local", "compose", ns, "service", "api")
    data_target = deployment_subject_id("local", "compose", ns, "service", "data")
    api_w = {"role": "workload", "platform": "compose", "sourceId": "local", "environment": "dev", "namespace": ns, "workloadKind": "service", "name": "api", "direction": "placement", "ports": []}
    data_w = {"role": "workload", "platform": "compose", "sourceId": "local", "environment": "dev", "namespace": ns, "workloadKind": "service", "name": "data", "direction": "placement", "ports": []}
    facts = _fact("API-W", "deployment", "required", "api", api_target, api_w)
    facts += _fact("DATA-W", "deployment", "required", "data", data_target, data_w)
    facts += _fact("NO-COLOCATE", "deployment", "forbidden", "api", "data", {"role": "co-location", "scope": "namespace", "environment": "dev"})
    facts += _fact("REQUIRE-ISOLATION", "deployment", "required", "api", "data", {"role": "isolation", "scope": "namespace", "environment": "dev"})
    root = _project(tmp_path, facts)
    _sources(root, _source("local", "compose", "compose.yaml", "dev"))
    _write(root / "compose.yaml", "services:\n  api: {x-sdai-component: api}\n  data: {x-sdai-component: data}\n")
    approved, observation = _observe(root)
    report = compare_architecture(approved, (observation,))
    assert any(item.code == "ARCH-DRIFT-FORBIDDEN-PRESENT" and item.approved_fact_id == "NO-COLOCATE" for item in report.findings)
    assert any(item.code == "ARCH-DRIFT-REQUIRED-MISSING" and item.approved_fact_id == "REQUIRE-ISOLATION" for item in report.findings)


def test_component_mapping_ambiguity_and_manifest_path_safety_fail_closed(tmp_path: Path) -> None:
    root = _project(tmp_path / "mapping")
    _sources(root, _source("api-k8s", "kubernetes", "src/api/deploy.yaml", "prod"))
    _write(root / "src" / "api" / "deploy.yaml", """apiVersion: apps/v1
kind: Deployment
metadata: {name: api, labels: {sdai.io/component: data}}
spec: {template: {metadata: {labels: {sdai.io/component: data}}, spec: {containers: []}}}
""")
    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DEPLOY-004.*ambiguous"):
        _observe(root)

    unsafe = _project(tmp_path / "unsafe")
    for path in ("../outside.yaml", "C:/deploy.yaml"):
        _sources(unsafe, _source("bad", "kubernetes", path, "prod"))
        with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DEPLOY-002"):
            load_deployment_sources(unsafe)


def test_duplicate_manifest_keys_fail_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
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


def test_workload_and_public_exposure_compose_with_security_engine(tmp_path: Path) -> None:
    target = deployment_subject_id("prod-k8s", "kubernetes", "prod-ns", "Deployment", "api")
    svc = deployment_subject_id("prod-k8s", "kubernetes", "prod-ns", "Service", "api-public")
    workload = {"role": "workload", "platform": "kubernetes", "sourceId": "prod-k8s", "environment": "prod", "namespace": "prod-ns", "workloadKind": "Deployment", "name": "api", "direction": "placement", "ports": []}
    exposure = {"role": "exposure", "platform": "kubernetes", "sourceId": "prod-k8s", "environment": "prod", "namespace": "prod-ns", "resourceKind": "Service", "name": "api-public", "exposure": "public", "direction": "inbound", "ports": [{"port": 443, "protocol": "TCP"}], "resource": svc}
    facts = _fact("API-W", "deployment", "allowed", "api", target, workload)
    facts += _fact("API-X", "deployment", "allowed", "external:public", "api", exposure)
    facts += _fact("ZONE-API", "trust-boundary", "allowed", "api", "zone:internal", {"role": "zone-membership"})
    facts += _fact("ZONE-WORKLOAD", "trust-boundary", "allowed", target, "zone:privileged", {"role": "zone-membership"})
    facts += _fact("DEPLOY-PLACEMENT", "trust-boundary", "allowed", "zone:internal", "zone:privileged", {"role": "boundary-rule", "evidenceKind": "deployment", "direction": "placement"})
    facts += _fact("DEPLOY-EXPOSURE", "trust-boundary", "allowed", "zone:external", "zone:internal", {"role": "boundary-rule", "evidenceKind": "deployment", "direction": "inbound"})
    root = _project(tmp_path, facts)
    _sources(root, _source("prod-k8s", "kubernetes", "deploy/api.yaml", "prod"))
    _write(root / "deploy" / "api.yaml", """apiVersion: apps/v1
kind: Deployment
metadata: {name: api, namespace: prod-ns, labels: {sdai.io/component: api}}
spec: {template: {metadata: {labels: {sdai.io/component: api}}, spec: {containers: []}}}
---
apiVersion: v1
kind: Service
metadata: {name: api-public, namespace: prod-ns, labels: {sdai.io/component: api}}
spec: {type: LoadBalancer, ports: [{port: 443}]}
""")
    approved, deployment = _observe(root)
    security = evaluate_trust_boundary_security(approved, (deployment,))
    security_findings = [item for item in security.findings if item.kind is ArchitectureFactKind.TRUST_BOUNDARY]
    assert security_findings == []
