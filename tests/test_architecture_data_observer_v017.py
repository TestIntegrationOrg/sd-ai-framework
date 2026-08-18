from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sdai.architecture_data_observer import DATA_OBSERVER_ID, RepositoryDataObserver
from sdai.architecture_drift import (
    ArchitectureDriftError,
    ArchitectureFactKind,
    compare_architecture,
    load_approved_architecture,
    load_architecture_topology,
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


FEATURE = "ARCH-DATA-219"
RESOURCE = "public.customers"
TARGET = "data:resource:public.customers"


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


def _topology(root: Path, *, with_data_policy: bool = True) -> None:
    facts = ""
    if with_data_policy:
        facts = "".join(
            (
                _fact(
                    "CUSTOMERS-OWNER",
                    kind="data-ownership",
                    mode="required",
                    source="orders",
                    target=TARGET,
                    attributes={"resource": RESOURCE, "resourceType": "table"},
                ),
                _fact(
                    "CUSTOMERS-ADMIN",
                    kind="data-access",
                    mode="required",
                    source="orders",
                    target=TARGET,
                    attributes={"resource": RESOURCE, "resourceType": "table", "access": "admin"},
                ),
                _fact(
                    "CUSTOMERS-REPORT-READ",
                    kind="data-access",
                    mode="allowed",
                    source="reporting",
                    target=TARGET,
                    attributes={"resource": RESOURCE, "resourceType": "table", "access": "read"},
                ),
                _fact(
                    "CUSTOMERS-REPORT-WRITE",
                    kind="data-access",
                    mode="forbidden",
                    source="reporting",
                    target=TARGET,
                    attributes={"resource": RESOURCE, "resourceType": "table", "access": "write"},
                ),
            )
        )
    approval = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    _write(
        root / "specs" / "changes" / FEATURE / "architecture" / "approved-topology.yaml",
        f"""apiVersion: sdai.architecture-topology/v1
kind: ApprovedArchitecture
metadata:
  id: data-topology
  feature: {FEATURE}
  approvalEvidence: {approval}
spec:
  components:
    - id: orders
      roots: [src/orders]
      modulePrefixes: [acme.orders]
    - id: reporting
      roots: [src/reporting]
      modulePrefixes: [acme.reporting]
  facts:
{facts if facts else '    []\n'}""",
    )


def _approve(root: Path) -> None:
    topology = load_architecture_topology(root, FEATURE)
    evidence_relative = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    record = TraceEvidence(
        evidence_id="ARCH-APPROVAL-219",
        kind=EvidenceKind.APPROVAL,
        status=EvidenceStatus.PASSED,
        subject=topology.subject,
        git_commit=_git(root, "rev-parse", "HEAD"),
        bindings=(EvidenceBinding(EvidenceBindingKind.ARTIFACT, topology.source, topology.file_sha256),),
        provenance=(TraceProvenance(evidence_relative, 1, detail="data topology approval"),),
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


def _project(tmp_path: Path, *, with_data_policy: bool = True) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "data-observer@example.invalid")
    _git(root, "config", "user.name", "Data Observer Tests")
    _topology(root, with_data_policy=with_data_policy)
    _write(root / "src" / "orders" / "placeholder.txt", "orders\n")
    _write(root / "src" / "reporting" / "placeholder.txt", "reporting\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "add approved data topology")
    _approve(root)
    return root


def _observe(root: Path):
    approved = load_approved_architecture(root, FEATURE)
    return approved, RepositoryDataObserver().observe(root, approved)


def test_sql_ownership_read_and_admin_match_approved_policy_deterministically(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(
        root / "src" / "orders" / "migrations" / "001.sql",
        """-- password='must-not-appear'
CREATE TABLE public.customers (
    id bigint primary key,
    name text
);
""",
    )
    _write(
        root / "src" / "reporting" / "report.sql",
        """SELECT c.id
FROM public.customers c
WHERE c.name <> 'secret-literal';
""",
    )

    approved, first = _observe(root)
    second = RepositoryDataObserver().observe(root, approved)

    assert first.observer_id == DATA_OBSERVER_ID
    assert first.to_json() == second.to_json()
    assert compare_architecture(approved, (first,)).findings == ()
    assert "must-not-appear" not in first.to_json()
    assert "secret-literal" not in first.to_json()

    ownership = [item for item in first.facts if item.kind is ArchitectureFactKind.DATA_OWNERSHIP]
    assert [(item.source, item.target) for item in ownership] == [("orders", TARGET)]
    accesses = {(item.source, dict(item.attributes)["access"]) for item in first.facts if item.kind is ArchitectureFactKind.DATA_ACCESS}
    assert ("orders", "admin") in accesses
    assert ("reporting", "read") in accesses


def test_cross_component_write_matches_forbidden_policy_with_source_provenance(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root / "src" / "orders" / "migrations" / "001.sql", "CREATE TABLE public.customers (id bigint);\n")
    _write(root / "src" / "reporting" / "mutation.sql", "UPDATE public.customers SET id = 2 WHERE id = 1;\n")

    approved, observation = _observe(root)
    report = compare_architecture(approved, (observation,))

    forbidden = [item for item in report.findings if item.code == "ARCH-DRIFT-FORBIDDEN-PRESENT"]
    assert len(forbidden) == 1
    assert forbidden[0].approved_fact_id == "CUSTOMERS-REPORT-WRITE"
    assert forbidden[0].observed_provenance[0].source == "src/reporting/mutation.sql"
    assert forbidden[0].observed_provenance[0].detail == "SQL UPDATE access"


def test_data_ownership_move_is_required_missing_and_unexpected_new_owner(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root / "src" / "reporting" / "migrations" / "001.sql", "CREATE TABLE public.customers (id bigint);\n")

    approved, observation = _observe(root)
    report = compare_architecture(approved, (observation,))
    ownership = [item for item in report.findings if item.kind is ArchitectureFactKind.DATA_OWNERSHIP]

    assert {item.code for item in ownership} == {
        "ARCH-DRIFT-REQUIRED-MISSING",
        "ARCH-DRIFT-UNEXPECTED-PRESENT",
    }
    unexpected = next(item for item in ownership if item.code == "ARCH-DRIFT-UNEXPECTED-PRESENT")
    assert unexpected.source == "reporting"
    assert unexpected.observed_provenance[0].source == "src/reporting/migrations/001.sql"


def test_orm_mappings_emit_read_and_write_without_claiming_ownership(tmp_path: Path) -> None:
    root = _project(tmp_path, with_data_policy=False)
    _write(root / "src" / "orders" / "model.py", "class Customer:\n    __tablename__ = 'customers'\n")
    _write(root / "src" / "orders" / "Customer.java", "@Table(name = \"customers\")\nclass Customer {}\n")
    _write(root / "src" / "orders" / "customer.ts", "@Entity('customers')\nexport class Customer {}\n")

    _, observation = _observe(root)
    access = [item for item in observation.facts if item.kind is ArchitectureFactKind.DATA_ACCESS]
    ownership = [item for item in observation.facts if item.kind is ArchitectureFactKind.DATA_OWNERSHIP]

    assert ownership == []
    assert {dict(item.attributes)["access"] for item in access} == {"read", "write"}
    for item in access:
        assert {prov.source for prov in item.provenance} == {
            "src/orders/Customer.java",
            "src/orders/customer.ts",
            "src/orders/model.py",
        }


def test_datasource_configuration_redacts_userinfo_passwords_and_query_secrets(tmp_path: Path) -> None:
    root = _project(tmp_path, with_data_policy=False)
    _write(
        root / "src" / "orders" / "application.properties",
        """spring.datasource.url=jdbc:postgresql://admin:TOPSECRET@db.internal:5432/orders?password=QUERYSECRET
spring.datasource.password=PROPERTYSECRET
""",
    )
    _write(
        root / "src" / "reporting" / "app.config",
        "ConnectionStrings:Main=Server=db.internal,1433;Database=analytics;User Id=sa;Password=DOTNETSECRET;\n",
    )

    _, observation = _observe(root)
    payload = observation.to_json()

    for secret in ("TOPSECRET", "QUERYSECRET", "PROPERTYSECRET", "DOTNETSECRET", "admin:TOPSECRET", "User Id=sa"):
        assert secret not in payload
    stores = [item for item in observation.facts if dict(item.attributes).get("resourceType") == "store"]
    assert len(stores) == 2
    postgres = next(item for item in stores if dict(item.attributes).get("storeKind") == "postgresql")
    assert postgres.target == "data:store:postgresql:db.internal:5432:orders"
    assert dict(postgres.attributes) == {
        "resourceType": "store",
        "storeKind": "postgresql",
        "host": "db.internal",
        "database": "orders",
        "access": "connect",
        "port": 5432,
    }


def test_dynamic_datasource_reference_is_explicit_stable_and_secret_safe(tmp_path: Path) -> None:
    root = _project(tmp_path, with_data_policy=False)
    _write(root / "src" / "orders" / "application.properties", "spring.datasource.url=${DATABASE_URL}\n")

    approved, first = _observe(root)
    second = RepositoryDataObserver().observe(root, approved)
    dynamic = [item for item in first.facts if dict(item.attributes).get("storeKind") == "dynamic"]

    assert len(dynamic) == 1
    assert dynamic[0].target == [item for item in second.facts if dict(item.attributes).get("storeKind") == "dynamic"][0].target
    assert dict(dynamic[0].attributes)["reference"] == "database_url"
    assert "${DATABASE_URL}" not in first.to_json()


def test_dynamic_sql_resource_fails_closed_instead_of_being_silently_ignored(tmp_path: Path) -> None:
    root = _project(tmp_path, with_data_policy=False)
    _write(root / "src" / "orders" / "dynamic.sql", "UPDATE ${TABLE_NAME} SET value = 1;\n")

    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DATA-003.*dynamic or unsupported resource"):
        _observe(root)


def test_sql_comment_and_string_literals_do_not_create_false_data_facts(tmp_path: Path) -> None:
    root = _project(tmp_path, with_data_policy=False)
    _write(
        root / "src" / "orders" / "comments.sql",
        """-- UPDATE public.fake SET x = 1;
/* CREATE TABLE public.also_fake (id int); */
SELECT 'FROM public.string_fake' AS value;
""",
    )

    _, observation = _observe(root)
    assert observation.facts == ()
