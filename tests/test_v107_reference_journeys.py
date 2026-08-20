from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from sdai.agent_platform import AgentRuntime
from sdai.architecture_artifact_validator import has_architecture_blockers, validate_architecture_artifacts
from sdai.artifact_state import record_artifact_state
from sdai.completion_barrier import complete_isolated_task, evaluate_task_completion
from sdai.convergence import ConvergenceStatus, RemediationKind, RemediationTask, run_convergence
from sdai.execution_ledger import create_execution_run, load_execution_run
from sdai.isolated_tasks import (
    IsolatedDispatch,
    IsolatedStage,
    IsolatedStageResult,
    IsolatedStageStatus,
    build_implementation_contract,
    build_isolated_invocation,
    build_review_contract,
    persist_stage_result,
    prepare_implementation_dispatch,
)
from sdai.models import FeatureContext, LifecycleMode
from sdai.multi_repo_pr_graph import build_multi_repo_feature_graph
from sdai.multi_repo_run import (
    MultiRepoExitClass,
    MultiRepoRunError,
    build_multi_repo_run_plan,
    execute_multi_repo_run,
    revalidate_run_plan,
)
from sdai.multi_repo_verify import verify_all_repositories
from sdai.policy import OperatingMode, load_effective_configuration
from sdai.pr_traceability import PR_EVIDENCE_API_VERSION
from sdai.scaffold import init_project
from sdai.specification_store_lifecycle import create_store, register_store
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance
from sdai.verification import (
    SemanticReviewDimension,
    SemanticReviewEvidence,
    VerificationCategory,
    VerificationFindingSource,
    VerificationOutcome,
    VerificationSeverity,
    VerificationStatus,
)
from sdai.v05_scaffold import install_v05_scaffold
from sdai.version_entrypoint import main as sdai_main
from sdai.workflow_execution import (
    WorkflowExecutionStatus,
    WorkflowLeafInvocation,
    WorkflowLeafOutcome,
    execute_workflow_graph,
)
from sdai.workflow_graph import load_workflow_graph


FEATURE_A = "REF-A-107"
FEATURE_B = "REF-B-107"
FEATURE_C = "REF-C-107"
FEATURE_D = "REF-D-107"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return (completed.stdout or "").strip()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _digest(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def _project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    init_project(root)
    install_v05_scaffold(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI 1.0 Reference Gate")
    _git(root, "config", "user.email", "sdai@example.test")


def _task(feature: str, token: str) -> RemediationTask:
    return RemediationTask(
        task_id=f"REMEDIATE-{token}",
        feature_id=feature,
        round_id=f"ROUND-{token}",
        verification_report_sha256="sha256:" + "1" * 64,
        verification_input_sha256="sha256:" + "2" * 64,
        finding_sha256="sha256:" + "3" * 64,
        finding_code="SDAI_VERIFY_TRACE_GAP",
        finding_source=VerificationFindingSource.DETERMINISTIC,
        category=VerificationCategory.TRACE_COVERAGE,
        severity=VerificationSeverity.BLOCKING,
        status=VerificationStatus.FAIL,
        subject="requirement:FR-001",
        summary="Implement FR-001 without changing specification truth.",
        remediation_kind=RemediationKind.IMPLEMENTATION,
        allowed_roots=("src", "tests"),
        forbidden_roots=(f"specs/changes/{feature}/requirements.md", "specs/current"),
        provenance=(TraceProvenance(f"specs/changes/{feature}/requirements.md", 3),),
    )


def _passed(prepared, root: Path, output: str) -> IsolatedStageResult:
    return IsolatedStageResult(
        prepared.record,
        IsolatedStageStatus.PASSED,
        _git(root, "rev-parse", "HEAD"),
        output,
    )


def _accepted_task(
    root: Path,
    ledger,
    task: RemediationTask,
    dispatch: IsolatedDispatch,
) -> tuple[IsolatedStageResult, IsolatedStageResult, IsolatedStageResult]:
    implementation = build_isolated_invocation(
        AgentRuntime(root), build_implementation_contract(root, task, dispatch)
    )
    impl_result = _passed(implementation, root, "implementation passed")
    persist_stage_result(root, implementation.contract, impl_result, ledger=ledger)

    specification = build_isolated_invocation(
        AgentRuntime(root),
        build_review_contract(root, task, impl_result, IsolatedStage.SPEC_COMPLIANCE_REVIEW),
    )
    spec_result = _passed(specification, root, "specification review passed")
    persist_stage_result(root, specification.contract, spec_result, ledger=ledger)

    quality = build_isolated_invocation(
        AgentRuntime(root),
        build_review_contract(
            root,
            task,
            impl_result,
            IsolatedStage.CODE_QUALITY_REVIEW,
            prior_review=spec_result,
        ),
    )
    quality_result = _passed(quality, root, "code quality review passed")
    persist_stage_result(root, quality.contract, quality_result, ledger=ledger)
    return impl_result, spec_result, quality_result


def _typed_evidence(
    root: Path,
    feature: str,
    evidence_id: str,
    kind: EvidenceKind,
    subject: str,
    source: str,
    result: dict[str, object],
) -> Path:
    record = TraceEvidence(
        evidence_id=evidence_id,
        kind=kind,
        status=EvidenceStatus.PASSED,
        subject=subject,
        git_commit=_git(root, "rev-parse", "HEAD"),
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.SOURCE,
                source,
                _digest(root / source),
            ),
        ),
        provenance=(TraceProvenance(source, 1),),
        producer=EvidenceProducer("sdai-reference-gate"),
        result=result,
    )
    return _write(
        root / "specs" / "changes" / feature / "evidence" / f"{evidence_id}.json",
        record.to_json() + "\n",
    )


def _semantic_review(
    root: Path,
    feature: str,
    review_id: str,
    dimension: SemanticReviewDimension,
    subject: str,
    context_name: str,
) -> Path:
    context = root / ".sdai" / "verification" / feature / "context" / f"{context_name}.txt"
    path = root / ".sdai" / "verification" / feature / "reviews" / f"{review_id}.json"
    evidence = TraceEvidence(
        evidence_id=review_id,
        kind=EvidenceKind.REVIEW,
        status=EvidenceStatus.PASSED,
        subject=subject,
        git_commit=_git(root, "rev-parse", "HEAD"),
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.EVIDENCE,
                context.relative_to(root).as_posix(),
                _digest(context),
            ),
        ),
        provenance=(TraceProvenance(path.relative_to(root).as_posix(), 1),),
        producer=EvidenceProducer("sdai-reference-reviewer"),
        result={"verdict": "passed", "dimension": dimension.value},
        tool="semantic-review",
    )
    review = SemanticReviewEvidence(
        review_id=review_id,
        dimension=dimension,
        subject=subject,
        summary=f"{dimension.value} review passed for {subject}.",
        evidence=evidence,
    )
    return _write(path, review.to_json() + "\n")


def _critical_architecture(context: FeatureContext) -> None:
    artifacts = {
        "specification.md": "# Specification\n\nFR-001 Process payment.\nNFR-001 Retry safely.\nAC-001 Retry is observable.\n",
        "rfc/RFC-001-retry.md": "# RFC-001 Retry\n\nStatus: Draft\n\n## Problem\nTransient failures require governed retry behavior.\n",
        "architecture/architecture.md": "# Architecture\n\n## Option A - synchronous retry\nTrade-offs.\n\n## Option B - queue retry\nTrade-offs.\n",
        "architecture/decision-matrix.md": "# Decision Matrix\n\n| Option | Reliability | Cost |\n|---|---|---|\n| A | Medium | Low |\n| B | High | Medium |\n",
        "adr/ADR-001-retry.md": "# ADR-001 Retry\n\nStatus: Proposed\n\n## Context\nChoose a retry mechanism.\n",
        "architecture/diagrams/context.puml": "@startuml\nactor User\nrectangle System\nUser --> System\n@enduml\n",
        "architecture/diagrams/component.drawio": "<mxfile><diagram><mxGraphModel><root><mxCell id=\"0\"/><mxCell id=\"1\" parent=\"0\"/><mxCell id=\"2\" value=\"Service\" vertex=\"1\" parent=\"1\"/></root></mxGraphModel></diagram></mxfile>\n",
        "architecture/diagrams/retry-sequence.puml": "@startuml\nactor User\nparticipant API\nUser -> API: request\n@enduml\n",
        "security/threat-model.md": "# Threat Model\n\nTrust boundary: client to API. Mitigation: authenticated requests and least privilege.\n",
        "contracts/openapi.yaml": "openapi: 3.1.0\ninfo:\n  title: Retry API\n  version: 1.0.0\npaths: {}\n",
        "tasks.yaml": "tasks:\n  - id: TASK-1\n    title: Implement retry\n    traces_to: [FR-001, NFR-001, AC-001]\n",
    }
    for relative, content in artifacts.items():
        _write(context.artifact(relative), content)


def test_journey_a_small_feature_reaches_fresh_completion(tmp_path: Path) -> None:
    root = tmp_path / "journey-a"
    _project(root)
    assert sdai_main(
        [
            "feature",
            FEATURE_A,
            "--title",
            "Small feature",
            "--description",
            "Reference lightweight lifecycle",
            "--path",
            str(root),
        ]
    ) == 0
    _write(
        root / "specs" / "changes" / FEATURE_A / "requirements.md",
        "# Requirements\n\n- FR-001: Return a deterministic greeting.\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "journey A specification")
    baseline = _git(root, "rev-parse", "HEAD")
    ledger = create_execution_run(root, FEATURE_A, "light", baseline, run_id="journey-a")
    task = _task(FEATURE_A, "a107a107a107a107")
    dispatch = prepare_implementation_dispatch(ledger, task)

    _write(root / "src" / "greeting.py", "# Trace: FR-001\ndef greeting() -> str:\n    return 'hello'\n")
    _write(root / "tests" / "test_greeting.py", "# Trace: FR-001\ndef test_greeting():\n    assert True\n")
    _git(root, "add", "src/greeting.py", "tests/test_greeting.py")
    _git(root, "commit", "-m", "implement journey A")
    chain = _accepted_task(root, ledger, task, dispatch)

    report = evaluate_task_completion(root, ledger, task, attempt=1, risk="trivial")
    assert report.passed
    assert all(item.git_commit == _git(root, "rev-parse", "HEAD") for item in chain)
    terminal = complete_isolated_task(root, ledger, task, attempt=1, risk="trivial")
    assert terminal.git_commit == _git(root, "rev-parse", "HEAD")
    assert ledger.reconstruct().task_map()[task.task_id].status == "completed"
    contracts = {
        event.payload.get("evidence_contract")
        for event in ledger.load_events()
        if event.kind == "task.evidence" and event.task_id == task.task_id
    }
    assert contracts == {
        "sdai.completion/spec-review/v1",
        "sdai.completion/code-quality-review/v1",
    }


def test_journey_b_critical_enterprise_governance_and_convergence(tmp_path: Path) -> None:
    root = tmp_path / "journey-b"
    _project(root)
    feature = root / "specs" / "changes" / FEATURE_B
    feature.mkdir(parents=True)
    context = FeatureContext(root, FEATURE_B)
    _critical_architecture(context)
    _write(
        feature / "requirements.md",
        "# Requirements\n\n- FR-001: Sign café scripts. TASK-001 implements it and TEST-001 verifies it.\n- NFR-001: Signing preserves Δ behavior. TASK-001 implements it and TEST-001 verifies it.\n",
    )
    _write(feature / "architecture.md", "# Architecture\n\nThe signing component implements FR-001 and NFR-001.\n")
    _write(feature / "plan.md", "# Plan\n\nImplement TASK-001 for FR-001 and NFR-001, then run TEST-001.\n")
    _write(feature / "tasks.md", "# Tasks\n\n- [ ] TASK-001: Implement FR-001 and NFR-001; verified by TEST-001.\n")
    _write(feature / "tests.md", "# Tests\n\n- TEST-001: Verify FR-001 and NFR-001 through TASK-001.\n")
    for name in ("FR-001", "NFR-001", "failure"):
        _write(
            root / ".sdai" / "verification" / FEATURE_B / "context" / f"{name}.txt",
            f"Semantic context for {name} café Δ.\n",
        )

    org_policy = tmp_path / "organization-policy.yaml"
    _write(
        org_policy,
        "version: 1\nproviders: {}\ncapabilities: {}\nexecution: {}\nskills: {}\narchitecture_validation:\n  allow_waivers: false\n",
    )
    completion_policy = tmp_path / "organization-completion-policy.yaml"
    _write(
        completion_policy,
        "apiVersion: sdai.completion-policy/v1\nrisks:\n  critical:\n    task: [approval]\n",
    )
    policy_env = {
        "SDAI_ORG_POLICY_PATH": str(org_policy.resolve()),
        "SDAI_ORG_COMPLETION_POLICY_PATH": str(completion_policy.resolve()),
    }
    effective = load_effective_configuration(root, environ=policy_env)
    assert effective.operating_mode is OperatingMode.ENTERPRISE
    assert effective.environment_allowlist == frozenset()
    assert effective.architecture_allow_waivers is False
    architecture = validate_architecture_artifacts(
        context,
        LifecycleMode.CRITICAL,
        effective_configuration=effective,
    )
    assert not has_architecture_blockers(architecture)

    # Durable execution still uses the stable specs/<feature> execution anchor while
    # specification truth lives in specs/changes/<feature>.
    _write(root / "specs" / FEATURE_B / "00-intake.md", "# durable execution anchor\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "journey B governed specification")
    baseline = _git(root, "rev-parse", "HEAD")
    ledger = create_execution_run(root, FEATURE_B, "enterprise", baseline, run_id="journey-b")
    task = _task(FEATURE_B, "b107b107b107b107")
    dispatch = prepare_implementation_dispatch(ledger, task)

    _write(
        root / "src" / "signing.py",
        "# Trace: FR-001 NFR-001 TASK-001 TEST-001\ndef sign() -> bool:\n    return True\n",
    )
    _write(
        root / "tests" / "test_signing.py",
        "# Trace: FR-001 NFR-001 TASK-001 TEST-001\ndef test_signing():\n    assert True\n",
    )
    _write(feature / "tasks.md", "# Tasks\n\n- [x] TASK-001: Implement FR-001 and NFR-001; verified by TEST-001.\n")
    for artifact_id in ("requirements", "architecture", "plan", "tasks", "tests"):
        record_artifact_state(root, FEATURE_B, artifact_id, risk="standard", environ={})
    _git(root, "add", ".")
    _git(root, "commit", "-m", "implement journey B")
    head = _git(root, "rev-parse", "HEAD")
    chain = _accepted_task(root, ledger, task, dispatch)
    assert all(item.git_commit == head for item in chain)

    evidence = {
        "test": _typed_evidence(root, FEATURE_B, "B-TEST", EvidenceKind.TEST, task.subject or "task", "src/signing.py", {"passed": True}),
        "quality": _typed_evidence(root, FEATURE_B, "B-QUALITY", EvidenceKind.QUALITY, task.subject or "task", "src/signing.py", {"qualityGate": "passed"}),
        "security": _typed_evidence(root, FEATURE_B, "B-SECURITY", EvidenceKind.SECURITY, task.subject or "task", "src/signing.py", {"securityGate": "passed"}),
        "approval": _typed_evidence(
            root,
            FEATURE_B,
            "B-APPROVAL",
            EvidenceKind.APPROVAL,
            task.subject or "task",
            "src/signing.py",
            {"mechanism": "local-manual-gate", "approved": True, "identityBacked": False},
        ),
    }
    completion = evaluate_task_completion(
        root,
        ledger,
        task,
        attempt=1,
        risk="critical",
        typed_evidence_paths=evidence,
        environ=policy_env,
    )
    assert completion.passed
    assert {item.dimension.value for item in completion.findings} >= {
        "spec-review",
        "code-quality-review",
        "test",
        "quality",
        "security",
        "approval",
    }
    complete_isolated_task(
        root,
        ledger,
        task,
        attempt=1,
        risk="critical",
        typed_evidence_paths=evidence,
        environ=policy_env,
    )
    approval_payload = json.loads(evidence["approval"].read_text(encoding="utf-8"))
    assert approval_payload["result"]["identityBacked"] is False

    for requirement in ("FR-001", "NFR-001"):
        _typed_evidence(
            root,
            FEATURE_B,
            f"B-EVIDENCE-{requirement}",
            EvidenceKind.TEST,
            f"requirement:{requirement}",
            "src/signing.py",
            {"passed": True},
        )
        _semantic_review(
            root,
            FEATURE_B,
            f"B-REVIEW-{requirement}",
            SemanticReviewDimension.REQUIREMENT_SATISFACTION,
            f"requirement:{requirement}",
            requirement,
        )
    first = run_convergence(root, FEATURE_B, risk="standard", max_rounds=3, environ={})
    assert first.status is ConvergenceStatus.ACTION_REQUIRED
    _semantic_review(
        root,
        FEATURE_B,
        "B-REVIEW-FAILURE",
        SemanticReviewDimension.FAILURE_BEHAVIOR,
        f"feature:{FEATURE_B}",
        "failure",
    )
    second = run_convergence(root, FEATURE_B, risk="standard", max_rounds=3, environ={})
    assert second.status is ConvergenceStatus.VERIFIED
    assert len(second.rounds) == 2
    assert ledger.reconstruct().task_map()[task.task_id].status == "completed"


def _participant(path: Path, prefix: str) -> tuple[Path, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "SDAI Multi Repo Gate")
    _git(path, "config", "user.email", "sdai@example.test")
    _write(path / ".sdai" / "config.yaml", "{}\n")
    feature = path / "specs" / "changes" / FEATURE_C
    requirement = f"FR-{prefix}-001"
    task = f"TASK-{prefix}-001"
    _write(feature / "requirements.md", f"# Requirements\n\n- {requirement}: {prefix} participant behavior.\n")
    _write(feature / "tasks.md", f"# Tasks\n\n- [x] {task}: Implement {requirement}.\n")
    _write(path / "src" / f"{prefix.casefold()}.py", f"# Trace links: {requirement} {task}\nREADY = True\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", f"implement {prefix} participant")
    return path, _git(path, "rev-parse", "HEAD")


def _pr_evidence(repository: Path, repository_id: str, prefix: str, implementation_commit: str) -> None:
    payload = {
        "apiVersion": PR_EVIDENCE_API_VERSION,
        "kind": "PullRequestEvidence",
        "featureId": FEATURE_C,
        "repositoryId": repository_id,
        "pullRequests": [
            {
                "id": f"review-{repository_id}",
                "headCommit": implementation_commit,
                "state": "open",
                "links": [f"task:TASK-{prefix}-001"],
                "provider": {
                    "name": "Reference Git Service",
                    "reference": repository_id,
                    "url": f"https://example.invalid/{repository_id}",
                },
            }
        ],
    }
    path = repository / "specs" / "changes" / FEATURE_C / "pr-evidence.yaml"
    _write(path, yaml.safe_dump(payload, sort_keys=False))
    _git(repository, "add", path.relative_to(repository).as_posix())
    _git(repository, "commit", "-m", "record PR trace evidence")


def test_journey_c_genuine_multi_repo_routing_execution_and_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "journey-c-project"
    project.mkdir()
    _write(project / ".sdai" / "config.yaml", "{}\n")
    store = tmp_path / "journey-c-store"
    create_store(store, "reference-specs", "1.0.0", description="Central reference specifications")
    change = store / "specs" / "changes" / FEATURE_C
    _write(
        change / "change.yaml",
        "version: 1\nfeature_id: REF-C-107\ntitle: Cross repository reference\ndescription: Coordinate API and UI\nstatus: proposed\ndomains: [api, ui]\nbaselines:\n  api: null\n  ui: null\n",
    )
    for domain, prefix in (("api", "API"), ("ui", "UI")):
        _write(
            change / "deltas" / f"{domain}.yaml",
            f"version: 1\ndomain: {domain}\nbaseline_spec_sha256: null\noperations:\n  - op: ADDED\n    requirement_id: FR-{prefix}-001\n    reason: Add {domain} behavior\n    definition: {prefix} repository owns this behavior.\n",
        )
    register_store(project, store)

    repositories: dict[str, Path] = {}
    for repository_id, prefix in (("api", "API"), ("ui", "UI")):
        repository, implementation_commit = _participant(tmp_path / f"journey-c-{repository_id}", prefix)
        repositories[repository_id] = repository
        _pr_evidence(repository, repository_id, prefix, implementation_commit)

    mapping = {
        "apiVersion": "sdai.feature-repositories/v1",
        "kind": "FeatureRepositories",
        "repositories": [
            {
                "id": repository_id,
                "path": str(repositories[repository_id].resolve()),
                "capabilities": ["requirements", "tasks"],
                "ownership": [
                    {"type": "requirement", "pattern": f"FR-{prefix}-*"},
                    {"type": "task", "pattern": f"TASK-{prefix}-*"},
                ],
                "required": True,
            }
            for repository_id, prefix in (("api", "API"), ("ui", "UI"))
        ],
    }
    _write(project / ".sdai" / "feature-repositories.yaml", yaml.safe_dump(mapping, sort_keys=False))

    graph = build_multi_repo_feature_graph(project, FEATURE_C)
    assert not graph.has_errors
    edges = {(edge.relation, edge.source, edge.target) for edge in graph.edges}
    assert ("owned-by", "requirement:FR-API-001", "repository:api") in edges
    assert ("owned-by", "requirement:FR-UI-001", "repository:ui") in edges
    assert ("included-in-pr", "task:TASK-API-001", "pr-reference:api:review-api") in edges
    assert ("included-in-pr", "task:TASK-UI-001", "pr-reference:ui:review-ui") in edges
    assert any(node.node_id == "store:reference-specs@1.0.0" for node in graph.nodes)

    plan = build_multi_repo_run_plan(project, FEATURE_C, isolation="worktree")
    assert plan.ready
    assert [item.repository_id for item in plan.participants] == ["api", "ui"]
    calls: list[str] = []

    def runner(participant, workflow: str, isolation: str) -> int:
        calls.append(participant.repository_id)
        assert isolation == "worktree"
        return 0

    result = execute_multi_repo_run(plan, runner)
    assert result.exit_class is MultiRepoExitClass.SUCCESS
    assert calls == ["api", "ui"]

    verified: list[str] = []

    class PassingReport:
        outcome = VerificationOutcome.PASSED

        def __init__(self, root: Path):
            self.root = root

        def to_json(self) -> str:
            return json.dumps({"outcome": "passed", "repository": self.root.name}, sort_keys=True)

    def pass_verify(root: Path, feature_id: str, *, risk: str, environ: dict[str, str]) -> PassingReport:
        verified.append(root.name)
        assert feature_id == FEATURE_C
        assert risk == "standard"
        assert environ == {}
        return PassingReport(root)

    monkeypatch.setattr("sdai.multi_repo_verify.verify_feature", pass_verify)
    verification = verify_all_repositories(project, FEATURE_C, risk="standard")
    assert verification.exit_class is MultiRepoExitClass.SUCCESS
    assert verified == [repositories["api"].name, repositories["ui"].name]
    assert [item.repository_id for item in verification.repositories] == ["api", "ui"]

    _write(repositories["ui"] / "post-plan-drift.txt", "conflicting local work\n")
    with pytest.raises(MultiRepoRunError, match="no longer clean/compatible|changed after"):
        revalidate_run_plan(plan)


def test_journey_d_interruption_resumes_without_duplicate_completed_work(tmp_path: Path) -> None:
    root = tmp_path / "journey-d"
    _write(root / ".sdai" / "config.yaml", "{}\n")
    _write(root / "specs" / FEATURE_D / "00-intake.md", "# Long running reference journey\n")
    _write(
        root / ".sdai" / "workflows" / "reference-long-running.yaml",
        yaml.safe_dump(
            {
                "version": 9,
                "name": "reference-long-running",
                "validation_mode": "standard",
                "steps": [
                    {"id": "one", "type": "deterministic", "action": "one"},
                    {"id": "two", "type": "deterministic", "action": "two"},
                    {"id": "three", "type": "deterministic", "action": "three"},
                ],
            },
            sort_keys=False,
        ),
    )
    resolution = load_workflow_graph(root, "reference-long-running")
    ledger = create_execution_run(root, FEATURE_D, "reference-long-running", "d" * 40, run_id="journey-d")
    calls: Counter[str] = Counter()
    interrupted_dispatches: list[str] = []

    def executor(invocation: WorkflowLeafInvocation) -> WorkflowLeafOutcome:
        calls[invocation.node.id] += 1
        if invocation.node.id == "two":
            interrupted_dispatches.append(invocation.dispatch_id)
            if calls["two"] == 1:
                raise RuntimeError("simulated process interruption")
        return WorkflowLeafOutcome(WorkflowExecutionStatus.SUCCEEDED, invocation.node.id.upper())

    with pytest.raises(RuntimeError, match="simulated process interruption"):
        execute_workflow_graph(resolution, ledger, leaf_executor=executor)

    before = ledger.load_events()
    completed_before = [event for event in before if event.kind == "task.completed"]
    assert len(completed_before) == 1
    first_completion = completed_before[0]
    # The interrupted attempt is allowed to leave a stale-but-durable checkpoint.
    # Capture its bytes without asking the ledger to treat it as current state.
    assert ledger.checkpoint_path.is_file()
    checkpoint_before = ledger.checkpoint_path.read_bytes()
    assert checkpoint_before

    resumed = load_execution_run(root, FEATURE_D, "journey-d")
    outcome = execute_workflow_graph(resolution, resumed, leaf_executor=executor)

    assert outcome.status is WorkflowExecutionStatus.SUCCEEDED
    assert calls == Counter({"two": 2, "one": 1, "three": 1})
    assert interrupted_dispatches[0] == interrupted_dispatches[1]
    assert resumed.reconstruct().status == "completed"
    final_events = resumed.load_events()
    preserved = next(event for event in final_events if event.event_id == first_completion.event_id)
    assert preserved.sha256 == first_completion.sha256
    completed_counts = Counter(event.task_id for event in final_events if event.kind == "task.completed")
    assert all(count == 1 for count in completed_counts.values())
    checkpoint_after = resumed.load_checkpoint()
    assert checkpoint_after is not None
    assert resumed.checkpoint_path.read_bytes() != checkpoint_before
