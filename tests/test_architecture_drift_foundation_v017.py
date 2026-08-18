from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess

import pytest

from sdai.architecture_drift import (
    ARCHITECTURE_DRIFT_API_VERSION,
    ARCHITECTURE_OBSERVATION_API_VERSION,
    ARCHITECTURE_TOPOLOGY_API_VERSION,
    ApprovedArchitecture,
    ArchitectureDriftError,
    ArchitectureFactKind,
    ArchitectureObservation,
    ArchitectureObserverRegistry,
    ObservedArchitectureFact,
    compare_architecture,
    load_approved_architecture,
    load_architecture_topology,
    resolve_architecture_workspace,
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


FEATURE = "ARCH-DRIFT-216"


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


def _init_git(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "architecture-drift@example.invalid")
    _git(root, "config", "user.name", "Architecture Drift Tests")


def _topology_text(*, approval: str, reversed_order: bool = False) -> str:
    components = """  - id: api
    roots: [src/api]
    modulePrefixes: [example.api]
  - id: data
    roots: [src/data]
    modulePrefixes: [example.data]
"""
    if reversed_order:
        components = """  - id: data
    roots: [src/data]
    modulePrefixes: [example.data]
  - id: api
    roots: [src/api]
    modulePrefixes: [example.api]
"""
    facts = """  - id: DEP-REQUIRED
    kind: dependency
    mode: required
    source: api
    target: data
    attributes:
      scope: runtime
  - id: DEP-FORBIDDEN
    kind: dependency
    mode: forbidden
    source: data
    target: api
    attributes:
      scope: runtime
  - id: COMM-ALLOWED
    kind: communication
    mode: allowed
    source: api
    target: data
    attributes:
      protocol: https
"""
    if reversed_order:
        facts = """  - id: COMM-ALLOWED
    kind: communication
    mode: allowed
    source: api
    target: data
    attributes:
      protocol: https
  - id: DEP-FORBIDDEN
    kind: dependency
    mode: forbidden
    source: data
    target: api
    attributes:
      scope: runtime
  - id: DEP-REQUIRED
    kind: dependency
    mode: required
    source: api
    target: data
    attributes:
      scope: runtime
"""
    return f"""apiVersion: {ARCHITECTURE_TOPOLOGY_API_VERSION}
kind: ApprovedArchitecture
metadata:
  id: topology-main
  feature: {FEATURE}
  approvalEvidence: {approval}
spec:
  components:
{components}  facts:
{facts}"""


def _workspace(root: Path, *, legacy: bool = False) -> Path:
    feature = root / "specs" / (FEATURE if legacy else f"changes/{FEATURE}")
    feature.mkdir(parents=True, exist_ok=True)
    return feature


def _prepare_topology(root: Path, *, legacy: bool = False, reversed_order: bool = False) -> tuple[Path, str]:
    feature = _workspace(root, legacy=legacy)
    evidence_relative = (
        f"specs/{FEATURE}/evidence/architecture-approval.json"
        if legacy
        else f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    )
    topology = _write(
        feature / "architecture" / "approved-topology.yaml",
        _topology_text(approval=evidence_relative, reversed_order=reversed_order),
    )
    return topology, evidence_relative


def _approve(
    root: Path,
    *,
    evidence_relative: str,
    provider: str | None = None,
    model: str | None = None,
    role: str = "architecture-approver",
    status: EvidenceStatus = EvidenceStatus.PASSED,
) -> Path:
    topology = load_architecture_topology(root, FEATURE)
    commit = _git(root, "rev-parse", "HEAD")
    evidence_path = root / evidence_relative
    record = TraceEvidence(
        evidence_id="ARCH-APPROVAL-216",
        kind=EvidenceKind.APPROVAL,
        status=status,
        subject=topology.subject,
        git_commit=commit,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.ARTIFACT,
                topology.source,
                topology.file_sha256,
            ),
        ),
        provenance=(
            TraceProvenance(
                evidence_relative,
                1,
                detail="human architecture topology approval",
            ),
        ),
        producer=EvidenceProducer(role, provider, model),
        result={
            "architectureApproval": {
                "featureId": FEATURE,
                "topologyId": topology.topology_id,
                "topologySha256": topology.sha256,
            }
        },
        tool="sdai-architecture-approval",
    )
    _write(evidence_path, record.to_json())
    return evidence_path


def _approved_project(tmp_path: Path) -> tuple[Path, ApprovedArchitecture]:
    root = tmp_path / "project"
    root.mkdir()
    _init_git(root)
    _, evidence_relative = _prepare_topology(root)
    _write(root / "src" / "api" / "service.py", "# api\n")
    _write(root / "src" / "data" / "repo.py", "# data\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "add approved topology")
    _approve(root, evidence_relative=evidence_relative)
    return root, load_approved_architecture(root, FEATURE)


def _observed(
    kind: ArchitectureFactKind,
    source: str,
    target: str,
    attributes: dict[str, object],
    source_path: str,
    line: int,
) -> ObservedArchitectureFact:
    return ObservedArchitectureFact(
        kind=kind,
        source=source,
        target=target,
        attributes=attributes,
        provenance=(TraceProvenance(source_path, line, detail="deterministic repository observation"),),
    )


def test_current_and_legacy_workspaces_resolve_but_dual_layout_fails_closed(tmp_path: Path) -> None:
    current = tmp_path / "current"
    current.mkdir()
    _prepare_topology(current)
    assert resolve_architecture_workspace(current, FEATURE) == current / "specs" / "changes" / FEATURE

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _prepare_topology(legacy, legacy=True)
    assert resolve_architecture_workspace(legacy, FEATURE) == legacy / "specs" / FEATURE

    ambiguous = tmp_path / "ambiguous"
    ambiguous.mkdir()
    _prepare_topology(ambiguous)
    _prepare_topology(ambiguous, legacy=True)
    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DRIFT-004.*ambiguous"):
        resolve_architecture_workspace(ambiguous, FEATURE)


def test_topology_semantic_hash_is_order_independent_and_json_is_stable(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    first_root.mkdir()
    _prepare_topology(first_root)
    first = load_architecture_topology(first_root, FEATURE)

    second_root = tmp_path / "second"
    second_root.mkdir()
    _prepare_topology(second_root, reversed_order=True)
    second = load_architecture_topology(second_root, FEATURE)

    assert first.sha256 == second.sha256
    assert first.truth_dict() == second.truth_dict()
    assert first.to_json() == load_architecture_topology(first_root, FEATURE).to_json()
    assert first.to_dict()["apiVersion"] == ARCHITECTURE_TOPOLOGY_API_VERSION
    assert [item.component_id for item in first.components] == ["api", "data"]
    assert [item.fact_id for item in first.facts] == ["COMM-ALLOWED", "DEP-FORBIDDEN", "DEP-REQUIRED"]


def test_human_hash_bound_approval_is_required_and_ai_self_approval_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _init_git(root)
    _, evidence_relative = _prepare_topology(root)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "add topology")

    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DRIFT-005"):
        load_approved_architecture(root, FEATURE)

    _approve(root, evidence_relative=evidence_relative, provider="openai", model="model-x")
    with pytest.raises(ArchitectureDriftError, match="cannot be self-approved"):
        load_approved_architecture(root, FEATURE)

    _approve(root, evidence_relative=evidence_relative, provider=None, model=None)
    approved = load_approved_architecture(root, FEATURE)
    assert approved.approval.producer.semantic_role == "architecture-approver"
    assert approved.freshness.satisfies_current_coverage is True
    assert approved.topology.file_sha256.startswith("sha256:")


def test_topology_change_invalidates_previously_valid_approval(tmp_path: Path) -> None:
    root, approved = _approved_project(tmp_path)
    topology_path = root / approved.topology.source
    topology_path.write_text(
        topology_path.read_text(encoding="utf-8").replace("scope: runtime", "scope: compile", 1),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ArchitectureDriftError, match="exact current topology file SHA-256"):
        load_approved_architecture(root, FEATURE)


def test_duplicate_yaml_keys_and_duplicate_component_ownership_fail_closed(tmp_path: Path) -> None:
    duplicate_key = tmp_path / "duplicate-key"
    duplicate_key.mkdir()
    feature = _workspace(duplicate_key)
    evidence = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    _write(
        feature / "architecture" / "approved-topology.yaml",
        _topology_text(approval=evidence).replace(
            f"  feature: {FEATURE}\n",
            f"  feature: {FEATURE}\n  feature: {FEATURE}\n",
        ),
    )
    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DRIFT-002"):
        load_architecture_topology(duplicate_key, FEATURE)

    duplicate_root = tmp_path / "duplicate-root"
    duplicate_root.mkdir()
    feature = _workspace(duplicate_root)
    _write(
        feature / "architecture" / "approved-topology.yaml",
        _topology_text(approval=evidence).replace("roots: [src/data]", "roots: [src/api]"),
    )
    with pytest.raises(ArchitectureDriftError, match="owned by both"):
        load_architecture_topology(duplicate_root, FEATURE)


def test_observer_registry_is_deterministic_and_rejects_duplicate_or_mismatched_ids(tmp_path: Path) -> None:
    root, approved = _approved_project(tmp_path)

    class Observer:
        observer_id = "repo-imports"

        def observe(self, project_root: Path, value: ApprovedArchitecture) -> ArchitectureObservation:
            assert project_root == root.resolve()
            assert value.topology.sha256 == approved.topology.sha256
            return ArchitectureObservation(
                self.observer_id,
                (
                    _observed(
                        ArchitectureFactKind.DEPENDENCY,
                        "api",
                        "data",
                        {"scope": "runtime"},
                        "src/api/service.py",
                        1,
                    ),
                ),
            )

    registry = ArchitectureObserverRegistry((Observer(),))
    first = registry.observe_all(root, approved)
    second = registry.observe_all(root, approved)
    assert [item.to_json() for item in first] == [item.to_json() for item in second]
    assert first[0].to_dict()["apiVersion"] == ARCHITECTURE_OBSERVATION_API_VERSION
    assert registry.observer_ids == ("repo-imports",)

    with pytest.raises(ArchitectureDriftError, match="duplicate architecture observer"):
        registry.register(Observer())
    with pytest.raises(ArchitectureDriftError, match="not registered"):
        registry.require("missing-observer")

    class Mismatch:
        observer_id = "declared-observer"

        def observe(self, project_root: Path, value: ApprovedArchitecture) -> ArchitectureObservation:
            return ArchitectureObservation("different-observer", ())

    mismatch = ArchitectureObserverRegistry((Mismatch(),))
    with pytest.raises(ArchitectureDriftError, match="mismatched observerId"):
        mismatch.observe_all(root, approved)


def test_comparator_reports_required_forbidden_and_unexpected_with_both_provenance_sides(tmp_path: Path) -> None:
    _, approved = _approved_project(tmp_path)
    observation = ArchitectureObservation(
        "foundation-fixture",
        (
            _observed(
                ArchitectureFactKind.DEPENDENCY,
                "data",
                "api",
                {"scope": "runtime"},
                "src/data/repo.py",
                7,
            ),
            _observed(
                ArchitectureFactKind.COMMUNICATION,
                "api",
                "data",
                {"protocol": "https"},
                "src/api/service.py",
                12,
            ),
            _observed(
                ArchitectureFactKind.CONTRACT,
                "api",
                "data",
                {"contract": "public-api"},
                "src/api/service.py",
                20,
            ),
        ),
    )

    first = compare_architecture(approved, (observation,))
    second = compare_architecture(approved, (observation,))
    assert first.to_json() == second.to_json()
    assert first.to_dict()["apiVersion"] == ARCHITECTURE_DRIFT_API_VERSION
    assert first.sha256 == second.sha256
    assert first.drifted is True

    by_code = {item.code: item for item in first.findings}
    assert set(by_code) == {
        "ARCH-DRIFT-REQUIRED-MISSING",
        "ARCH-DRIFT-FORBIDDEN-PRESENT",
        "ARCH-DRIFT-UNEXPECTED-PRESENT",
    }
    required = by_code["ARCH-DRIFT-REQUIRED-MISSING"]
    assert required.approved_fact_id == "DEP-REQUIRED"
    assert required.approved_provenance
    assert not required.observed_provenance

    forbidden = by_code["ARCH-DRIFT-FORBIDDEN-PRESENT"]
    assert forbidden.approved_fact_id == "DEP-FORBIDDEN"
    assert forbidden.approved_provenance
    assert forbidden.observed_provenance[0].source == "src/data/repo.py"

    unexpected = by_code["ARCH-DRIFT-UNEXPECTED-PRESENT"]
    assert unexpected.approved_fact_id is None
    assert unexpected.approved_provenance[0].source == approved.topology.source
    assert unexpected.observed_provenance[0].source == "src/api/service.py"


def test_matching_required_and_allowed_facts_produce_no_drift(tmp_path: Path) -> None:
    _, approved = _approved_project(tmp_path)
    observation = ArchitectureObservation(
        "matching-fixture",
        (
            _observed(
                ArchitectureFactKind.DEPENDENCY,
                "api",
                "data",
                {"scope": "runtime"},
                "src/api/service.py",
                1,
            ),
            _observed(
                ArchitectureFactKind.COMMUNICATION,
                "api",
                "data",
                {"protocol": "https"},
                "src/api/service.py",
                2,
            ),
        ),
    )
    report = compare_architecture(approved, (observation,))
    assert report.findings == ()
    assert report.drifted is False


def test_unsafe_windows_or_traversal_component_roots_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "unsafe"
    root.mkdir()
    feature = _workspace(root)
    evidence = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    text = _topology_text(approval=evidence).replace("roots: [src/api]", "roots: [../outside]")
    _write(feature / "architecture" / "approved-topology.yaml", text)
    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DRIFT-003"):
        load_architecture_topology(root, FEATURE)

    text = _topology_text(approval=evidence).replace("roots: [src/api]", "roots: [CON/config]")
    _write(feature / "architecture" / "approved-topology.yaml", text)
    with pytest.raises(ArchitectureDriftError, match="reserved Windows"):
        load_architecture_topology(root, FEATURE)
