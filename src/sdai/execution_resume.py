from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from typing import Mapping
from uuid import uuid4

from sdai.execution_ledger import (
    ExecutionLedger,
    ExecutionLedgerError,
    HashBinding,
    LedgerEvent,
    load_execution_run,
)
from sdai.models import validate_feature_id


class ExecutionResumeError(RuntimeError):
    """Raised when current repository state cannot support a trustworthy resume."""


_GIT_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DISPATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DANGEROUS_GIT_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
}


def _fail(code: str, message: str) -> ExecutionResumeError:
    return ExecutionResumeError(f"{code}: {message}")


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-RESUME-001", f"resume data is not canonical JSON: {exc}") from exc


def _sha256_payload(payload: Mapping[str, object]) -> str:
    return "sha256:" + sha256(_canonical_bytes(payload)).hexdigest()


def _git_executable() -> str:
    candidate = shutil.which("git")
    if not candidate:
        raise _fail("SDAI-RESUME-002", "Git executable is not available")
    resolved = Path(candidate).resolve()
    if not resolved.is_file():
        raise _fail("SDAI-RESUME-002", f"resolved Git executable is not a file: {resolved}")
    return str(resolved)


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if (
            upper in _DANGEROUS_GIT_ENV
            or upper.startswith("GIT_CONFIG_KEY_")
            or upper.startswith("GIT_CONFIG_VALUE_")
            or upper == "GIT_CONFIG_COUNT"
        ):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [_git_executable(), *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        env=_git_env(),
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        raise _fail("SDAI-RESUME-002", f"git {' '.join(args)} failed: {detail}")
    return completed


def _git_output(root: Path, *args: str) -> str:
    return (_git(root, *args).stdout or "").strip()


def _repository_identity(root: Path, feature_id: str) -> tuple[str, bool, str]:
    top = Path(_git_output(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root.resolve():
        raise _fail(
            "SDAI-RESUME-002",
            f"SDAI project root must be the Git repository root; project={root.resolve()} git={top}",
        )
    head = _git_output(root, "rev-parse", "HEAD").casefold()
    if not _GIT_COMMIT.fullmatch(head):
        raise _fail("SDAI-RESUME-002", f"current Git HEAD is not a full commit identity: {head!r}")
    execution_glob = f"specs/{feature_id}/.sdai/execution/**"
    status = (
        _git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".",
            f":(exclude){execution_glob}",
        ).stdout
        or ""
    )
    return head, not bool(status.strip()), status


def _commit_is_ancestor(root: Path, recorded: str, head: str) -> tuple[bool, str | None]:
    completed = _git(root, "merge-base", "--is-ancestor", recorded, head, check=False)
    if completed.returncode == 0:
        return True, None
    detail = (completed.stderr or completed.stdout or "recorded commit is not reachable from HEAD").strip()
    if completed.returncode == 1:
        return False, "recorded_commit_not_ancestor"
    return False, "recorded_commit_unavailable:" + detail.replace("\n", " ")[:240]


@dataclass(frozen=True)
class BindingVerification:
    kind: str
    source: str
    expected_sha256: str
    actual_sha256: str | None
    valid: bool
    reason: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "source": self.source,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "valid": self.valid,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TaskResumeDecision:
    task_id: str
    registration_sequence: int
    attempt: int
    current_status: str
    action: str
    skip_verified: bool
    recorded_commit: str | None
    git_reachable: bool | None
    bindings: tuple[BindingVerification, ...]
    reasons: tuple[str, ...]
    existing_dispatch_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "registration_sequence": self.registration_sequence,
            "attempt": self.attempt,
            "current_status": self.current_status,
            "action": self.action,
            "skip_verified": self.skip_verified,
            "recorded_commit": self.recorded_commit,
            "git_reachable": self.git_reachable,
            "bindings": [item.as_dict() for item in self.bindings],
            "reasons": list(self.reasons),
            "existing_dispatch_id": self.existing_dispatch_id,
        }


@dataclass(frozen=True)
class ResumePlan:
    run_id: str
    feature_id: str
    run_status: str
    current_head: str
    repository_clean: bool
    repository_status: str
    checkpoint_status: str
    last_sequence: int
    last_sha256: str
    task_order: tuple[str, ...]
    tasks: tuple[TaskResumeDecision, ...]
    resume_task_id: str | None
    resume_action: str | None
    blocked_reason: str | None
    plan_sha256: str

    @property
    def resumable(self) -> bool:
        return self.blocked_reason is None

    def _body(self) -> dict[str, object]:
        return {
            "apiVersion": "sdai.execution-resume-plan/v1",
            "run_id": self.run_id,
            "feature_id": self.feature_id,
            "run_status": self.run_status,
            "current_head": self.current_head,
            "repository_clean": self.repository_clean,
            "repository_status": self.repository_status,
            "checkpoint_status": self.checkpoint_status,
            "last_sequence": self.last_sequence,
            "last_sha256": self.last_sha256,
            "task_order": list(self.task_order),
            "tasks": [item.as_dict() for item in self.tasks],
            "resume_task_id": self.resume_task_id,
            "resume_action": self.resume_action,
            "blocked_reason": self.blocked_reason,
        }

    def as_dict(self) -> dict[str, object]:
        body = self._body()
        body["plan_sha256"] = self.plan_sha256
        return body

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@dataclass(frozen=True)
class ResumeResult:
    plan: ResumePlan
    status: str
    dispatch_id: str | None
    dispatch_reused: bool
    checkpoint_path: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": "sdai.execution-resume-result/v1",
            "status": self.status,
            "dispatch_id": self.dispatch_id,
            "dispatch_reused": self.dispatch_reused,
            "checkpoint_path": self.checkpoint_path,
            "plan": self.plan.as_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _checkpoint_status(ledger: ExecutionLedger) -> str:
    if not ledger.checkpoint_path.exists():
        return "missing"
    try:
        checkpoint = ledger.load_checkpoint()
    except ExecutionLedgerError as exc:
        message = str(exc)
        if "checkpoint is stale relative to the current event ledger" in message:
            return "stale"
        raise
    return "current" if checkpoint is not None else "missing"


def _event_task_order(events: tuple[LedgerEvent, ...]) -> tuple[tuple[str, int], ...]:
    result: list[tuple[str, int]] = []
    seen: set[str] = set()
    for event in events:
        if event.kind != "task.registered" or event.task_id is None:
            continue
        if event.task_id in seen:
            raise _fail(
                "SDAI-RESUME-004",
                f"task registration order is ambiguous because '{event.task_id}' was registered twice",
            )
        seen.add(event.task_id)
        result.append((event.task_id, event.sequence))
    return tuple(result)


def _attempts_and_reservations(
    events: tuple[LedgerEvent, ...],
) -> tuple[dict[str, int], dict[tuple[str, int], str]]:
    attempts: dict[str, int] = {}
    reservations: dict[tuple[str, int], str] = {}
    for event in events:
        task = event.task_id
        if task is None:
            continue
        if event.kind == "task.registered":
            attempts[task] = 1
        elif event.kind == "task.reopened":
            attempts[task] = attempts.get(task, 1) + 1
        elif event.kind == "task.dispatch_reserved":
            attempt = attempts.get(task)
            dispatch_id = event.payload.get("dispatch_id")
            payload_attempt = event.payload.get("attempt")
            if (
                attempt is None
                or not isinstance(dispatch_id, str)
                or not _DISPATCH_ID.fullmatch(dispatch_id)
                or not isinstance(payload_attempt, int)
                or isinstance(payload_attempt, bool)
                or payload_attempt != attempt
            ):
                raise _fail(
                    "SDAI-RESUME-004",
                    f"invalid dispatch reservation event {event.event_id}",
                )
            reservations[(task, attempt)] = dispatch_id
    return attempts, reservations


def _verify_binding(ledger: ExecutionLedger, binding: HashBinding) -> BindingVerification:
    path = ledger.project_root.joinpath(*PurePosixPath(binding.source).parts)
    try:
        current = ledger.binding_for_file(path, kind=binding.kind)
    except ExecutionLedgerError as exc:
        return BindingVerification(
            binding.kind,
            binding.source,
            binding.sha256,
            None,
            False,
            "binding_unavailable:" + str(exc),
        )
    valid = current.sha256 == binding.sha256
    return BindingVerification(
        binding.kind,
        binding.source,
        binding.sha256,
        current.sha256,
        valid,
        None if valid else "binding_hash_mismatch",
    )


def _decision_for_task(
    ledger: ExecutionLedger,
    root: Path,
    head: str,
    task_id: str,
    registration_sequence: int,
    attempt: int,
    state,
    existing_dispatch_id: str | None,
) -> TaskResumeDecision:
    status = state.status
    if status == "completed":
        reasons: list[str] = []
        reachable: bool | None = None
        if state.git_commit is None:
            reasons.append("missing_completion_commit")
        else:
            reachable, reason = _commit_is_ancestor(root, state.git_commit, head)
            if not reachable and reason:
                reasons.append(reason)
        verifications = tuple(_verify_binding(ledger, item) for item in state.bindings)
        if not state.bindings:
            reasons.append("missing_completion_bindings")
        for verification in verifications:
            if not verification.valid:
                reasons.append(verification.reason or "binding_invalid")
        verified = not reasons
        return TaskResumeDecision(
            task_id,
            registration_sequence,
            attempt,
            status,
            "skip" if verified else "reopen",
            verified,
            state.git_commit,
            reachable,
            verifications,
            tuple(reasons),
            None,
        )
    if status == "failed":
        return TaskResumeDecision(
            task_id,
            registration_sequence,
            attempt,
            status,
            "retry",
            False,
            state.git_commit,
            None,
            (),
            ("task_failed",),
            None,
        )
    if status == "started":
        return TaskResumeDecision(
            task_id,
            registration_sequence,
            attempt,
            status,
            "resume",
            False,
            state.git_commit,
            None,
            (),
            ("task_interrupted_after_start",),
            existing_dispatch_id,
        )
    if status == "registered":
        return TaskResumeDecision(
            task_id,
            registration_sequence,
            attempt,
            status,
            "dispatch",
            False,
            state.git_commit,
            None,
            (),
            ("task_not_started",),
            existing_dispatch_id,
        )
    raise _fail("SDAI-RESUME-004", f"unsupported task state for '{task_id}': {status!r}")


def build_resume_plan(project_root: Path, feature_id: str, run_id: str) -> ResumePlan:
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    ledger = load_execution_run(root, feature, run_id)
    events = ledger.load_events()
    state = ledger.reconstruct()
    checkpoint = _checkpoint_status(ledger)
    head, clean, status_text = _repository_identity(root, feature)
    ordered = _event_task_order(events)
    attempts, reservations = _attempts_and_reservations(events)
    task_map = state.task_map()

    decisions: list[TaskResumeDecision] = []
    for task_id, sequence in ordered:
        task_state = task_map.get(task_id)
        if task_state is None:
            raise _fail("SDAI-RESUME-004", f"registered task '{task_id}' is missing from reconstructed state")
        attempt = attempts.get(task_id, 1)
        decisions.append(
            _decision_for_task(
                ledger,
                root,
                head,
                task_id,
                sequence,
                attempt,
                task_state,
                reservations.get((task_id, attempt)),
            )
        )

    resume_decision = next((item for item in decisions if item.action != "skip"), None)
    blocked_reason: str | None = None
    if state.status not in {"active", "paused"}:
        blocked_reason = f"run_terminal:{state.status}"
    elif not clean:
        blocked_reason = "repository_dirty_outside_execution_state"

    body: dict[str, object] = {
        "apiVersion": "sdai.execution-resume-plan/v1",
        "run_id": run_id,
        "feature_id": feature,
        "run_status": state.status,
        "current_head": head,
        "repository_clean": clean,
        "repository_status": status_text,
        "checkpoint_status": checkpoint,
        "last_sequence": state.last_sequence,
        "last_sha256": state.last_sha256,
        "task_order": [task for task, _ in ordered],
        "tasks": [item.as_dict() for item in decisions],
        "resume_task_id": resume_decision.task_id if resume_decision else None,
        "resume_action": resume_decision.action if resume_decision else None,
        "blocked_reason": blocked_reason,
    }
    digest = _sha256_payload(body)
    return ResumePlan(
        run_id=run_id,
        feature_id=feature,
        run_status=state.status,
        current_head=head,
        repository_clean=clean,
        repository_status=status_text,
        checkpoint_status=checkpoint,
        last_sequence=state.last_sequence,
        last_sha256=state.last_sha256,
        task_order=tuple(task for task, _ in ordered),
        tasks=tuple(decisions),
        resume_task_id=resume_decision.task_id if resume_decision else None,
        resume_action=resume_decision.action if resume_decision else None,
        blocked_reason=blocked_reason,
        plan_sha256=digest,
    )


def _checkpoint_resume(
    ledger: ExecutionLedger,
    plan: ResumePlan,
    dispatch_id: str | None,
    *,
    dispatch_reused: bool,
) -> str:
    ledger.write_checkpoint(
        {
            "resume": {
                "plan_sha256": plan.plan_sha256,
                "resume_task_id": plan.resume_task_id,
                "resume_action": plan.resume_action,
                "dispatch_id": dispatch_id,
                "dispatch_reused": dispatch_reused,
                "current_head": plan.current_head,
                "task_order": list(plan.task_order),
            }
        }
    )
    return ledger.checkpoint_path.relative_to(ledger.project_root).as_posix()


def resume_execution(
    project_root: Path,
    feature_id: str,
    run_id: str,
    *,
    max_compare_retries: int = 4,
) -> ResumeResult:
    root = project_root.resolve()
    for _ in range(max_compare_retries):
        plan = build_resume_plan(root, feature_id, run_id)
        if plan.blocked_reason is not None:
            return ResumeResult(plan, "blocked", None, False, None)
        if plan.resume_task_id is None:
            ledger = load_execution_run(root, plan.feature_id, plan.run_id)
            checkpoint = _checkpoint_resume(ledger, plan, None, dispatch_reused=False)
            return ResumeResult(plan, "nothing-to-resume", None, False, checkpoint)

        selected = next(item for item in plan.tasks if item.task_id == plan.resume_task_id)
        ledger = load_execution_run(root, plan.feature_id, plan.run_id)
        expected = plan.last_sha256
        try:
            if plan.run_status == "paused":
                event = ledger.append_event(
                    "run.resumed",
                    payload={"reason": "execution_resume"},
                    expected_last_sha256=expected,
                )
                expected = event.sha256

            if selected.action in {"reopen", "retry"}:
                event = ledger.append_event(
                    "task.reopened",
                    task_id=selected.task_id,
                    payload={
                        "reason": "stale_completion_evidence"
                        if selected.action == "reopen"
                        else "retry_failed_task",
                        "previous_status": selected.current_status,
                        "resume_plan_sha256": plan.plan_sha256,
                        "verification_reasons": list(selected.reasons),
                    },
                    expected_last_sha256=expected,
                )
                expected = event.sha256
                selected_attempt = selected.attempt + 1
                existing_dispatch = None
            else:
                selected_attempt = selected.attempt
                existing_dispatch = selected.existing_dispatch_id

            if existing_dispatch is not None:
                updated = build_resume_plan(root, plan.feature_id, plan.run_id)
                checkpoint = _checkpoint_resume(
                    ledger,
                    updated,
                    existing_dispatch,
                    dispatch_reused=True,
                )
                return ResumeResult(updated, "ready", existing_dispatch, True, checkpoint)

            dispatch_id = (
                f"dispatch:{plan.run_id}:{selected.task_id}:{selected_attempt}:"
                f"{uuid4().hex}"
            )
            event = ledger.append_event(
                "task.dispatch_reserved",
                task_id=selected.task_id,
                payload={
                    "dispatch_id": dispatch_id,
                    "attempt": selected_attempt,
                    "current_head": plan.current_head,
                    "resume_plan_sha256": plan.plan_sha256,
                },
                expected_last_sha256=expected,
            )
            expected = event.sha256
            updated = build_resume_plan(root, plan.feature_id, plan.run_id)
            checkpoint = _checkpoint_resume(
                ledger,
                updated,
                dispatch_id,
                dispatch_reused=False,
            )
            return ResumeResult(updated, "ready", dispatch_id, False, checkpoint)
        except ExecutionLedgerError as exc:
            if "SDAI-LEDGER-008" in str(exc):
                continue
            raise
    raise _fail(
        "SDAI-RESUME-005",
        "resume state changed repeatedly while reserving dispatch; retry after the competing process settles",
    )
