from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sdai.architecture_communication_observer import (
    COMMUNICATION_OBSERVER_ID,
    ServiceCommunicationObserver,
)
from sdai.architecture_drift import (
    ArchitectureComponent,
    ArchitectureDriftError,
    ArchitectureFactKind,
    compare_architecture,
    load_approved_architecture,
    load_architecture_topology,
)
from sdai.architecture_repository import ArchitectureRepositoryIndex
from sdai.contract_trace import build_contract_trace_index
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance


FEATURE = "ARCH-COMM-218"


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


def _init(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "communication-observer@example.invalid")
    _git(root, "config", "user.name", "Communication Observer Tests")


def _contracts(root: Path) -> tuple[str, str]:
    _write(
        root / ".sdai" / "contracts.yaml",
        """apiVersion: sdai.contract-sources/v1
kind: ContractSources
sources:
  - id: public-api
    kind: openapi
    path: contracts/public-api.yaml
""",
    )
    _write(
        root / "contracts" / "public-api.yaml",
        """openapi: 3.1.0
info:
  title: Public API
  version: 1.0.0
paths:
  /pets:
    get:
      operationId: listPets
      responses:
        '200':
          description: ok
""",
    )
    index = build_contract_trace_index(root)
    source = index.sources["public-api"]
    symbol = index.symbols[("public-api", "/paths/~1pets/get")]
    source_sha = source.metadata["source_sha256"]
    assert isinstance(source_sha, str)
    return source_sha, symbol.symbol_sha256


def _fact(
    fact_id: str,
    *,
    kind: str,
    source: str,
    target: str,
    attributes: dict[str, object],
    mode: str = "required",
) -> str:
    attrs = json.dumps(attributes, sort_keys=True, separators=(",", ":"))
    return f"""    - id: {fact_id}
      kind: {kind}
      mode: {mode}
      source: {source}
      target: {target}
      attributes: {attrs}
"""


def _topology(root: Path, *, include_contract: bool = True, ambiguous_host: bool = False) -> None:
    source_sha, symbol_sha = _contracts(root) if include_contract else ("", "")
    facts = "".join(
        (
            _fact(
                "HTTP-PY",
                kind="communication",
                source="api",
                target="endpoint:http",
                attributes={"direction": "inbound", "protocol": "http", "method": "GET", "endpoint": "/py"},
            ),
            _fact(
                "HTTP-JAVA",
                kind="communication",
                source="api",
                target="endpoint:http",
                attributes={"direction": "inbound", "protocol": "http", "method": "GET", "endpoint": "/java"},
            ),
            _fact(
                "HTTP-DOTNET",
                kind="communication",
                source="api",
                target="endpoint:http",
                attributes={"direction": "inbound", "protocol": "http", "method": "GET", "endpoint": "/dotnet"},
            ),
            _fact(
                "HTTP-JS",
                kind="communication",
                source="api",
                target="endpoint:http",
                attributes={"direction": "inbound", "protocol": "http", "method": "GET", "endpoint": "/js"},
            ),
            _fact(
                "HTTP-GO",
                kind="communication",
                source="api",
                target="endpoint:http",
                attributes={"direction": "inbound", "protocol": "http", "method": "ANY", "endpoint": "/go"},
            ),
            _fact(
                "HTTP-OUT",
                kind="communication",
                source="api",
                target="data",
                attributes={
                    "direction": "outbound",
                    "protocol": "http",
                    "method": "GET",
                    "endpoint": "/health",
                    "host": "data.internal",
                    "transport": "https",
                },
            ),
            _fact(
                "EVENT-PUBLISH",
                kind="communication",
                source="api",
                target="data",
                attributes={
                    "direction": "outbound",
                    "protocol": "event",
                    "action": "publish",
                    "channel": "users.signed",
                },
            ),
        )
    )
    if ambiguous_host:
        facts += _fact(
            "HTTP-AMBIGUOUS",
            kind="communication",
            source="api",
            target="audit",
            attributes={
                "direction": "outbound",
                "protocol": "http",
                "method": "GET",
                "endpoint": "/audit",
                "host": "data.internal",
                "transport": "https",
            },
            mode="allowed",
        )
    if include_contract:
        facts += _fact(
            "CONTRACT-PUBLIC",
            kind="contract",
            source="api",
            target="contract:public-api",
            attributes={
                "sourceId": "public-api",
                "sourceSha256": source_sha,
                "address": "/paths/~1pets/get",
                "symbolSha256": symbol_sha,
            },
        )

    components = """    - id: api
      roots: [src/api]
      modulePrefixes: [acme.api]
    - id: data
      roots: [src/data]
      modulePrefixes: [acme.data]
"""
    if ambiguous_host:
        components += """    - id: audit
      roots: [src/audit]
      modulePrefixes: [acme.audit]
"""
    approval = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    _write(
        root / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml",
        f"""apiVersion: sdai.architecture-topology/v1
kind: ApprovedArchitecture
metadata:
  id: communication-topology
  feature: {FEATURE}
  approvalEvidence: {approval}
spec:
  components:
{components}  facts:
{facts}""",
    )


def _approve(root: Path) -> None:
    topology = load_architecture_topology(root, FEATURE)
    evidence_relative = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    record = TraceEvidence(
        evidence_id="ARCH-APPROVAL-218",
        kind=EvidenceKind.APPROVAL,
        status=EvidenceStatus.PASSED,
        subject=topology.subject,
        git_commit=_git(root, "rev-parse", "HEAD"),
        bindings=(EvidenceBinding(EvidenceBindingKind.ARTIFACT, topology.source, topology.file_sha256),),
        provenance=(TraceProvenance(evidence_relative, 1, detail="communication topology approval"),),
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


def _project(tmp_path: Path, *, include_contract: bool = True, ambiguous_host: bool = False) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _init(root)
    _topology(root, include_contract=include_contract, ambiguous_host=ambiguous_host)
    _write(root / "src" / "data" / "placeholder.txt", "data\n")
    if ambiguous_host:
        _write(root / "src" / "audit" / "placeholder.txt", "audit\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "add communication topology")
    _approve(root)
    return root


def _observe(root: Path):
    approved = load_approved_architecture(root, FEATURE)
    return approved, ServiceCommunicationObserver().observe(root, approved)


def test_cross_framework_http_event_and_contract_facts_match_approved_topology(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(
        root / "src" / "api" / "app.py",
        """@app.get('/py')
def py():
    return requests.get('https://data.internal/health')

producer.send('users.signed')
""",
    )
    _write(
        root / "src" / "api" / "Api.java",
        """// @GetMapping("/ignored")
@GetMapping("/java")
public String java() { kafkaTemplate.send("users.signed", "x"); return "ok"; }
""",
    )
    _write(root / "src" / "api" / "Api.cs", "[HttpGet(\"/dotnet\")]\npublic string Get() => \"ok\";\n")
    _write(
        root / "src" / "api" / "app.ts",
        """// app.get('/ignored', handler)
app.get('/js', handler);
const fake = \"fetch('https://ignored.invalid/no')\";
producer.publish('users.signed', payload);
""",
    )
    _write(root / "src" / "api" / "main.go", "package api\nhttp.HandleFunc(\"/go\", handler)\n")
    _write(root / "src" / "api" / "check.ps1", "Invoke-RestMethod -Uri 'https://data.internal/health' -Method Get\n")

    approved, observation = _observe(root)
    assert observation.observer_id == COMMUNICATION_OBSERVER_ID
    assert observation.to_json() == ServiceCommunicationObserver().observe(root, approved).to_json()
    assert compare_architecture(approved, (observation,)).findings == ()

    communication = [item for item in observation.facts if item.kind is ArchitectureFactKind.COMMUNICATION]
    contracts = [item for item in observation.facts if item.kind is ArchitectureFactKind.CONTRACT]
    assert len(communication) == 7
    assert len(contracts) == 1

    outgoing = next(item for item in communication if item.target == "data" and dict(item.attributes).get("host"))
    assert {item.source for item in outgoing.provenance} == {"src/api/app.py", "src/api/check.ps1"}

    event = next(item for item in communication if dict(item.attributes).get("protocol") == "event")
    assert len(event.provenance) == 3
    assert {item.source for item in event.provenance} == {
        "src/api/Api.java",
        "src/api/app.py",
        "src/api/app.ts",
    }

    contract = contracts[0]
    assert contract.target == "contract:public-api"
    assert contract.provenance[0].source == "contracts/public-api.yaml"
    assert contract.provenance[0].declaration_sha256 == dict(contract.attributes)["sourceSha256"]


def test_contract_symbol_change_reports_missing_and_unexpected_binding(tmp_path: Path) -> None:
    root = _project(tmp_path)
    approved = load_approved_architecture(root, FEATURE)
    _write(
        root / "contracts" / "public-api.yaml",
        """openapi: 3.1.0
info:
  title: Public API
  version: 2.0.0
paths:
  /pets:
    get:
      operationId: listPetsV2
      responses:
        '200':
          description: ok
""",
    )
    report = compare_architecture(approved, (ServiceCommunicationObserver().observe(root, approved),))
    contract_findings = [item for item in report.findings if item.kind is ArchitectureFactKind.CONTRACT]
    assert {item.code for item in contract_findings} == {
        "ARCH-DRIFT-REQUIRED-MISSING",
        "ARCH-DRIFT-UNEXPECTED-PRESENT",
    }


def test_removed_contract_symbol_leaves_required_binding_missing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    approved = load_approved_architecture(root, FEATURE)
    _write(
        root / "contracts" / "public-api.yaml",
        """openapi: 3.1.0
info:
  title: Public API
  version: 2.0.0
paths: {}
""",
    )
    report = compare_architecture(approved, (ServiceCommunicationObserver().observe(root, approved),))
    contract_findings = [item for item in report.findings if item.kind is ArchitectureFactKind.CONTRACT]
    assert len(contract_findings) == 1
    assert contract_findings[0].code == "ARCH-DRIFT-REQUIRED-MISSING"
    assert contract_findings[0].approved_fact_id == "CONTRACT-PUBLIC"


def test_dynamic_http_targets_fail_closed(tmp_path: Path) -> None:
    root = _project(tmp_path, include_contract=False)
    _write(root / "src" / "api" / "dynamic.py", "url = get_url()\nrequests.get(url)\n")
    with pytest.raises(ArchitectureDriftError, match="dynamic Python HTTP client URL"):
        _observe(root)

    (root / "src" / "api" / "dynamic.py").unlink()
    _write(root / "src" / "api" / "dynamic.ts", "fetch(serviceUrl);\n")
    with pytest.raises(ArchitectureDriftError, match="dynamic JavaScript/TypeScript HTTP target"):
        _observe(root)


def test_external_target_is_stable_and_unexpected(tmp_path: Path) -> None:
    root = _project(tmp_path, include_contract=False)
    _write(root / "src" / "api" / "external.py", "requests.get('https://vendor.example/v1/status')\n")
    approved, first = _observe(root)
    second = ServiceCommunicationObserver().observe(root, approved)
    first_external = next(item for item in first.facts if item.target.startswith("external:http:"))
    second_external = next(item for item in second.facts if item.target.startswith("external:http:"))
    assert first_external.target == second_external.target
    report = compare_architecture(approved, (first,))
    assert any(item.code == "ARCH-DRIFT-UNEXPECTED-PRESENT" and item.target == first_external.target for item in report.findings)


def test_conflicting_approved_host_aliases_fail_closed(tmp_path: Path) -> None:
    root = _project(tmp_path, include_contract=False, ambiguous_host=True)
    _write(root / "src" / "api" / "client.py", "requests.get('https://data.internal/health')\n")
    with pytest.raises(ArchitectureDriftError, match="host alias.*multiple targets"):
        _observe(root)


def test_repository_index_uses_longest_component_root(tmp_path: Path) -> None:
    root = tmp_path / "ownership"
    root.mkdir()
    index = ArchitectureRepositoryIndex(
        root,
        (
            ArchitectureComponent("shell", ("src",), ()),
            ArchitectureComponent("data", ("src/data",), ()),
        ),
    )
    _write(root / "src" / "data" / "client.py", "# data\n")
    assert index.owner_for_relative_path("src/data/client.py") == "data"
    assert index.owner_for_relative_path("src/app/service.py") == "shell"
