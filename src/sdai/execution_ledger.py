from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Mapping
from uuid import uuid4

from sdai.models import validate_feature_id
from sdai.path_safety import ensure_within_project


class ExecutionLedgerError(RuntimeError):
    """Raised when durable execution state is invalid, corrupt, or unsafe."""


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WORKFLOW = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GIT_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_BINDING_KINDS = frozenset({"artifact", "evidence"})
_EVENT_KINDS = frozenset(
    {
        "run.created",
        "run.paused",
        "run.resumed",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "task.registered",
        "task.started",
        "task.implementation",
        "task.review",
        "task.evidence",
        "task.completed",
        "task.failed",
    }
)
_TASK_TERMINAL = frozenset({"completed", "failed"})
_RUN_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_EVENT_KEYS = frozenset(
    {
        "apiVersion",
        "sequence",
        "event_id",
        "run_id",
        "feature_id",
        "kind",
        "task_id",
        "recorded_at",
        "git_commit",
        "bindings",
        "payload",
        "previous_sha256",
        "sha256",
    }
)
_CHECKPOINT_KEYS = frozenset(
    {
        "apiVersion",
        "run_id",
        "feature_id",
        "last_sequence",
        "last_sha256",
        "state",
        "extra",
        "sha256",
    }
)
_ZERO_HASH = "sha256:" + ("0" * 64)


def _fail(code: str, message: str) -> ExecutionLedgerError:
    return ExecutionLedgerError(f"{code}: {message}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        text = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-LEDGER-002", f"record is not finite JSON data: {exc}") from exc
    return text.encode("utf-8")


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise _fail("SDAI-LEDGER-007", "operating system returned a short/zero append write")
        view = view[written:]


def _validate_json_value(value: object, *, label: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail("SDAI-LEDGER-002", f"{label} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, label=f"{label}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _fail("SDAI-LEDGER-002", f"{label} mapping keys must be strings")
            _validate_json_value(item, label=f"{label}.{key}")
        return
    raise _fail("SDAI-LEDGER-002", f"{label} contains unsupported type {type(value).__name__}")


def _validate_portable_source(source: str) -> str:
    if not isinstance(source, str) or not source.strip():
        raise _fail("SDAI-LEDGER-002", "binding source must be a non-empty string")
    if "\\" in source or source.startswith("/") or re.match(r"^[A-Za-z]:", source):
        raise _fail("SDAI-LEDGER-002", f"binding source must be repository-relative POSIX path: {source!r}")
    path = PurePosixPath(source)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise _fail("SDAI-LEDGER-002", f"binding source is not a safe repository-relative path: {source!r}")
    return path.as_posix()


def _safe_existing_path(project_root: Path, path: Path, *, label: str) -> Path:
    root = project_root.resolve()
    candidate = ensure_within_project(root, path, label=label)
    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise _fail("SDAI-LEDGER-003", f"{label} contains symlink component: {current}")
    return candidate


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    _validate_json_value(payload, label="record")
    _atomic_write(path, _canonical_bytes(payload) + b"\n")


def _atomic_text(path: Path, content: str) -> None:
    if not isinstance(content, str):
        raise _fail("SDAI-LEDGER-002", "task brief content must be text")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    _atomic_write(path, normalized.encode("utf-8"))


def _validate_commit(value: str | None, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise _fail("SDAI-LEDGER-005", "task completion requires a Git commit binding")
        return None
    normalized = value.strip().casefold()
    if not _GIT_COMMIT.fullmatch(normalized):
        raise _fail("SDAI-LEDGER-002", f"invalid Git commit identity: {value!r}")
    return normalized


def _validate_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail("SDAI-LEDGER-004", "event recorded_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _fail("SDAI-LEDGER-004", f"invalid event timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise _fail("SDAI-LEDGER-004", "event timestamp must include timezone")
    return value


@dataclass(frozen=True)
class HashBinding:
    kind: str
    source: str
    sha256: str

    def __post_init__(self) -> None:
        if self.kind not in _BINDING_KINDS:
            raise _fail("SDAI-LEDGER-002", f"unsupported binding kind: {self.kind!r}")
        object.__setattr__(self, "source", _validate_portable_source(self.source))
        if not _SHA256.fullmatch(self.sha256):
            raise _fail("SDAI-LEDGER-002", f"invalid SHA-256 binding: {self.sha256!r}")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "source": self.source, "sha256": self.sha256}

    @classmethod
    def from_mapping(cls, raw: object) -> "HashBinding":
        if not isinstance(raw, Mapping) or set(raw) != {"kind", "source", "sha256"}:
            raise _fail("SDAI-LEDGER-004", "event binding must contain exactly kind/source/sha256")
        return cls(str(raw["kind"]), str(raw["source"]), str(raw["sha256"]))


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    feature_id: str
    workflow: str
    baseline_commit: str
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": "sdai.execution-run/v1",
            "run_id": self.run_id,
            "feature_id": self.feature_id,
            "workflow": self.workflow,
            "baseline_commit": self.baseline_commit,
            "created_at": self.created_at,
        }

    @property
    def sha256(self) -> str:
        return _sha256_bytes(_canonical_bytes(self.as_dict()))


@dataclass(frozen=True)
class LedgerEvent:
    sequence: int
    event_id: str
    run_id: str
    feature_id: str
    kind: str
    task_id: str | None
    recorded_at: str
    git_commit: str | None
    bindings: tuple[HashBinding, ...]
    payload: dict[str, object]
    previous_sha256: str
    sha256: str

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": "sdai.execution-event/v1",
            "sequence": self.sequence,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "feature_id": self.feature_id,
            "kind": self.kind,
            "task_id": self.task_id,
            "recorded_at": self.recorded_at,
            "git_commit": self.git_commit,
            "bindings": [item.as_dict() for item in self.bindings],
            "payload": self.payload,
            "previous_sha256": self.previous_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        result = self.body_dict()
        result["sha256"] = self.sha256
        return result

    @classmethod
    def from_mapping(cls, raw: object) -> "LedgerEvent":
        if not isinstance(raw, Mapping) or set(raw) != _EVENT_KEYS:
            raise _fail("SDAI-LEDGER-004", "event record fields do not match sdai.execution-event/v1")
        if raw.get("apiVersion") != "sdai.execution-event/v1":
            raise _fail("SDAI-LEDGER-004", "unsupported execution event apiVersion")
        sequence = raw.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise _fail("SDAI-LEDGER-004", "event sequence must be a positive integer")
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in _EVENT_KINDS:
            raise _fail("SDAI-LEDGER-004", f"unsupported event kind: {kind!r}")
        task_id = raw.get("task_id")
        if task_id is not None and (not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id)):
            raise _fail("SDAI-LEDGER-004", f"invalid task_id in event: {task_id!r}")
        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            raise _fail("SDAI-LEDGER-004", "event payload must be a mapping")
        payload_dict = dict(payload)
        _validate_json_value(payload_dict, label="event payload")
        bindings_raw = raw.get("bindings")
        if not isinstance(bindings_raw, list):
            raise _fail("SDAI-LEDGER-004", "event bindings must be a list")
        bindings = tuple(HashBinding.from_mapping(item) for item in bindings_raw)
        keys = [(item.kind, item.source) for item in bindings]
        if len(keys) != len(set(keys)):
            raise _fail("SDAI-LEDGER-004", "event contains duplicate binding kind/source entries")
        git_commit_raw = raw.get("git_commit")
        if git_commit_raw is not None and not isinstance(git_commit_raw, str):
            raise _fail("SDAI-LEDGER-004", "event git_commit must be a string or null")
        git_commit = _validate_commit(git_commit_raw)
        previous = raw.get("previous_sha256")
        digest = raw.get("sha256")
        if not isinstance(previous, str) or not _SHA256.fullmatch(previous):
            raise _fail("SDAI-LEDGER-004", "event previous_sha256 is invalid")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise _fail("SDAI-LEDGER-004", "event sha256 is invalid")
        run_id = raw.get("run_id")
        feature_id = raw.get("feature_id")
        event_id = raw.get("event_id")
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise _fail("SDAI-LEDGER-004", "event run_id is invalid")
        if not isinstance(feature_id, str):
            raise _fail("SDAI-LEDGER-004", "event feature_id is invalid")
        feature_id = validate_feature_id(feature_id)
        expected_event_id = f"{run_id}:{sequence:08d}"
        if event_id != expected_event_id:
            raise _fail(
                "SDAI-LEDGER-004",
                f"event_id mismatch; expected {expected_event_id!r}, got {event_id!r}",
            )
        return cls(
            sequence=sequence,
            event_id=expected_event_id,
            run_id=run_id,
            feature_id=feature_id,
            kind=kind,
            task_id=task_id,
            recorded_at=_validate_timestamp(str(raw.get("recorded_at") or "")),
            git_commit=git_commit,
            bindings=bindings,
            payload=payload_dict,
            previous_sha256=previous,
            sha256=digest,
        )


@dataclass(frozen=True)
class TaskExecutionState:
    task_id: str
    status: str
    terminal_event_id: str | None = None
    git_commit: str | None = None
    bindings: tuple[HashBinding, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "terminal_event_id": self.terminal_event_id,
            "git_commit": self.git_commit,
            "bindings": [item.as_dict() for item in self.bindings],
        }


@dataclass(frozen=True)
class ExecutionState:
    run_id: str
    feature_id: str
    status: str
    last_sequence: int
    last_sha256: str
    tasks: tuple[TaskExecutionState, ...]

    def task_map(self) -> dict[str, TaskExecutionState]:
        return {item.task_id: item for item in self.tasks}

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": "sdai.execution-state/v1",
            "run_id": self.run_id,
            "feature_id": self.feature_id,
            "status": self.status,
            "last_sequence": self.last_sequence,
            "last_sha256": self.last_sha256,
            "tasks": [item.as_dict() for item in self.tasks],
        }


class _LedgerLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_LedgerLock":
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise _fail(
                "SDAI-LEDGER-006",
                f"execution ledger is locked by another process: {self.path}",
            ) from exc
        payload = f"pid={os.getpid()} acquired_at={_now().isoformat()}\n".encode("utf-8")
        _write_all(self.fd, payload)
        os.fsync(self.fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.path.unlink(missing_ok=True)


class ExecutionLedger:
    def __init__(self, project_root: Path, manifest: RunManifest) -> None:
        self.project_root = project_root.resolve()
        self.manifest = manifest
        self.run_dir = _safe_existing_path(
            self.project_root,
            self.project_root
            / "specs"
            / manifest.feature_id
            / ".sdai"
            / "execution"
            / manifest.run_id,
            label="execution run directory",
        )
        self.manifest_path = self.run_dir / "run.json"
        self.events_path = self.run_dir / "events.jsonl"
        self.checkpoint_path = self.run_dir / "checkpoint.json"
        self.lock_path = self.run_dir / "ledger.lock"

    def _lock(self) -> _LedgerLock:
        return _LedgerLock(self.lock_path)

    def task_record_paths(self, task_id: str) -> dict[str, Path]:
        task = self._validate_task_id(task_id)
        root = _safe_existing_path(
            self.project_root,
            self.run_dir / "tasks" / task,
            label=f"task '{task}' record directory",
        )
        return {
            "brief": root / "brief.md",
            "implementation": root / "implementation.json",
            "review": root / "review.json",
            "evidence": root / "evidence.json",
        }

    def _validate_task_id(self, task_id: str) -> str:
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise _fail("SDAI-LEDGER-002", f"invalid task id: {task_id!r}")
        return task_id

    def binding_for_file(self, path: Path, *, kind: str = "artifact") -> HashBinding:
        candidate = _safe_existing_path(self.project_root, path, label="execution evidence file")
        if candidate.is_symlink() or not candidate.is_file():
            raise _fail("SDAI-LEDGER-003", f"binding source must be a regular non-symlink file: {candidate}")
        source = candidate.relative_to(self.project_root).as_posix()
        return HashBinding(kind, source, _sha256_bytes(candidate.read_bytes()))

    def write_task_brief(self, task_id: str, content: str) -> HashBinding:
        task = self._validate_task_id(task_id)
        with self._lock():
            state = self._reconstruct_unlocked()
            task_state = state.task_map().get(task)
            if task_state is None:
                raise _fail("SDAI-LEDGER-005", f"task '{task}' is not registered")
            if task_state.status in _TASK_TERMINAL:
                raise _fail("SDAI-LEDGER-005", f"task '{task}' is already terminal")
            path = self.task_record_paths(task)["brief"]
            _atomic_text(path, content)
            return self.binding_for_file(path, kind="evidence")

    def write_task_record(
        self,
        task_id: str,
        record_type: str,
        payload: Mapping[str, object],
    ) -> HashBinding:
        task = self._validate_task_id(task_id)
        if record_type not in {"implementation", "review", "evidence"}:
            raise _fail("SDAI-LEDGER-002", f"unsupported task record type: {record_type!r}")
        if not isinstance(payload, Mapping):
            raise _fail("SDAI-LEDGER-002", "task record payload must be a mapping")
        payload_dict = dict(payload)
        _validate_json_value(payload_dict, label=f"task {record_type} payload")
        with self._lock():
            state = self._reconstruct_unlocked()
            task_state = state.task_map().get(task)
            if task_state is None:
                raise _fail("SDAI-LEDGER-005", f"task '{task}' is not registered")
            if task_state.status in _TASK_TERMINAL:
                raise _fail("SDAI-LEDGER-005", f"task '{task}' is already terminal")
            path = self.task_record_paths(task)[record_type]
            document = {
                "apiVersion": f"sdai.execution-{record_type}/v1",
                "run_id": self.manifest.run_id,
                "feature_id": self.manifest.feature_id,
                "task_id": task,
                "payload": payload_dict,
            }
            _atomic_json(path, document)
            return self.binding_for_file(path, kind="evidence")

    def append_event(
        self,
        kind: str,
        *,
        task_id: str | None = None,
        git_commit: str | None = None,
        bindings: tuple[HashBinding, ...] = (),
        payload: Mapping[str, object] | None = None,
    ) -> LedgerEvent:
        if kind not in _EVENT_KINDS:
            raise _fail("SDAI-LEDGER-002", f"unsupported event kind: {kind!r}")
        task = self._validate_task_id(task_id) if task_id is not None else None
        commit = _validate_commit(git_commit)
        payload_dict = dict(payload or {})
        _validate_json_value(payload_dict, label="event payload")
        keys = [(item.kind, item.source) for item in bindings]
        if len(keys) != len(set(keys)):
            raise _fail("SDAI-LEDGER-002", "event bindings contain duplicate kind/source entries")
        with self._lock():
            events = self._load_events_unlocked()
            state = self._reconstruct_events(events)
            self._validate_transition(state, kind, task, commit, bindings)
            sequence = len(events) + 1
            previous = events[-1].sha256 if events else _ZERO_HASH
            body = {
                "apiVersion": "sdai.execution-event/v1",
                "sequence": sequence,
                "event_id": f"{self.manifest.run_id}:{sequence:08d}",
                "run_id": self.manifest.run_id,
                "feature_id": self.manifest.feature_id,
                "kind": kind,
                "task_id": task,
                "recorded_at": _now().isoformat(),
                "git_commit": commit,
                "bindings": [item.as_dict() for item in bindings],
                "payload": payload_dict,
                "previous_sha256": previous,
            }
            digest = _sha256_bytes(_canonical_bytes(body))
            event = LedgerEvent(
                sequence=sequence,
                event_id=str(body["event_id"]),
                run_id=self.manifest.run_id,
                feature_id=self.manifest.feature_id,
                kind=kind,
                task_id=task,
                recorded_at=str(body["recorded_at"]),
                git_commit=commit,
                bindings=bindings,
                payload=payload_dict,
                previous_sha256=previous,
                sha256=digest,
            )
            self._append_line_unlocked(event)
            return event

    def _append_line_unlocked(self, event: LedgerEvent) -> None:
        if self.events_path.exists() and self.events_path.is_symlink():
            raise _fail("SDAI-LEDGER-003", "events.jsonl must not be a symlink")
        line = _canonical_bytes(event.as_dict()) + b"\n"
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.events_path, flags, 0o600)
        try:
            _write_all(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def load_events(self) -> tuple[LedgerEvent, ...]:
        with self._lock():
            return self._load_events_unlocked()

    def _load_events_unlocked(self) -> tuple[LedgerEvent, ...]:
        if not self.events_path.exists():
            return ()
        if self.events_path.is_symlink() or not self.events_path.is_file():
            raise _fail("SDAI-LEDGER-003", "events.jsonl must be a regular non-symlink file")
        raw = self.events_path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise _fail("SDAI-LEDGER-004", "events.jsonl ends with a truncated/incomplete record")
        events: list[LedgerEvent] = []
        previous = _ZERO_HASH
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                raise _fail("SDAI-LEDGER-004", f"events.jsonl contains blank record at line {line_number}")
            try:
                decoded = line.decode("utf-8")
                payload = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _fail("SDAI-LEDGER-004", f"invalid JSONL record at line {line_number}: {exc}") from exc
            event = LedgerEvent.from_mapping(payload)
            expected_sequence = len(events) + 1
            if event.sequence != expected_sequence:
                raise _fail(
                    "SDAI-LEDGER-004",
                    f"event sequence gap/duplicate at line {line_number}: expected {expected_sequence}, got {event.sequence}",
                )
            if event.run_id != self.manifest.run_id or event.feature_id != self.manifest.feature_id:
                raise _fail("SDAI-LEDGER-004", f"event identity mismatch at line {line_number}")
            if event.previous_sha256 != previous:
                raise _fail("SDAI-LEDGER-004", f"event hash chain mismatch at line {line_number}")
            expected_hash = _sha256_bytes(_canonical_bytes(event.body_dict()))
            if event.sha256 != expected_hash:
                raise _fail("SDAI-LEDGER-004", f"event content hash mismatch at line {line_number}")
            events.append(event)
            previous = event.sha256
        self._reconstruct_events(tuple(events))
        return tuple(events)

    def _validate_transition(
        self,
        state: ExecutionState,
        kind: str,
        task_id: str | None,
        git_commit: str | None,
        bindings: tuple[HashBinding, ...],
    ) -> None:
        if state.status in _RUN_TERMINAL:
            raise _fail("SDAI-LEDGER-005", f"run is already terminal ({state.status})")
        if kind.startswith("task.") and task_id is None:
            raise _fail("SDAI-LEDGER-005", f"event '{kind}' requires task_id")
        if not kind.startswith("task.") and task_id is not None:
            raise _fail("SDAI-LEDGER-005", f"run event '{kind}' must not include task_id")
        tasks = state.task_map()
        if kind == "run.created":
            if state.last_sequence != 0:
                raise _fail("SDAI-LEDGER-005", "run.created may appear only as the first event")
            return
        if state.last_sequence == 0:
            raise _fail("SDAI-LEDGER-005", "run.created must be the first event")
        if state.status == "paused" and kind not in {"run.resumed", "run.failed", "run.cancelled"}:
            raise _fail("SDAI-LEDGER-005", "paused run must be resumed before additional task/run activity")
        if kind == "run.paused":
            if state.status != "active":
                raise _fail("SDAI-LEDGER-005", f"run cannot pause from status {state.status}")
            return
        if kind == "run.resumed":
            if state.status != "paused":
                raise _fail("SDAI-LEDGER-005", f"run cannot resume from status {state.status}")
            return
        if kind == "task.registered":
            assert task_id is not None
            if task_id in tasks:
                raise _fail("SDAI-LEDGER-005", f"task '{task_id}' is already registered")
            return
        if kind.startswith("task."):
            assert task_id is not None
            current = tasks.get(task_id)
            if current is None:
                raise _fail("SDAI-LEDGER-005", f"task '{task_id}' is not registered")
            if current.status in _TASK_TERMINAL:
                raise _fail("SDAI-LEDGER-005", f"task '{task_id}' is already terminal ({current.status})")
            if kind == "task.started":
                if current.status != "registered":
                    raise _fail("SDAI-LEDGER-005", f"task '{task_id}' cannot start from status {current.status}")
                return
            if current.status != "started":
                raise _fail("SDAI-LEDGER-005", f"event '{kind}' requires task '{task_id}' to be started")
            if kind == "task.completed":
                _validate_commit(git_commit, required=True)
                if not bindings:
                    raise _fail(
                        "SDAI-LEDGER-005",
                        f"task '{task_id}' completion requires at least one artifact/evidence hash binding",
                    )
            return
        if kind == "run.completed":
            incomplete = sorted(task.task_id for task in tasks.values() if task.status != "completed")
            if incomplete:
                raise _fail(
                    "SDAI-LEDGER-005",
                    "run cannot complete while tasks are incomplete: " + ", ".join(incomplete),
                )

    def _reconstruct_events(self, events: tuple[LedgerEvent, ...]) -> ExecutionState:
        run_status = "new"
        tasks: dict[str, TaskExecutionState] = {}
        previous = _ZERO_HASH
        for index, event in enumerate(events):
            if event.sequence != index + 1:
                raise _fail("SDAI-LEDGER-004", "event sequence is not strictly monotonic")
            if event.previous_sha256 != previous:
                raise _fail("SDAI-LEDGER-004", "event hash chain is not contiguous")
            provisional = ExecutionState(
                run_id=self.manifest.run_id,
                feature_id=self.manifest.feature_id,
                status=run_status,
                last_sequence=index,
                last_sha256=previous,
                tasks=tuple(sorted(tasks.values(), key=lambda item: item.task_id)),
            )
            self._validate_transition(
                provisional,
                event.kind,
                event.task_id,
                event.git_commit,
                event.bindings,
            )
            if event.kind == "run.created":
                run_status = "active"
            elif event.kind == "run.paused":
                run_status = "paused"
            elif event.kind == "run.resumed":
                run_status = "active"
            elif event.kind == "run.completed":
                run_status = "completed"
            elif event.kind == "run.failed":
                run_status = "failed"
            elif event.kind == "run.cancelled":
                run_status = "cancelled"
            elif event.kind == "task.registered":
                assert event.task_id is not None
                tasks[event.task_id] = TaskExecutionState(event.task_id, "registered")
            elif event.kind == "task.started":
                assert event.task_id is not None
                tasks[event.task_id] = TaskExecutionState(event.task_id, "started")
            elif event.kind == "task.completed":
                assert event.task_id is not None
                tasks[event.task_id] = TaskExecutionState(
                    event.task_id,
                    "completed",
                    event.event_id,
                    event.git_commit,
                    event.bindings,
                )
            elif event.kind == "task.failed":
                assert event.task_id is not None
                tasks[event.task_id] = TaskExecutionState(
                    event.task_id,
                    "failed",
                    event.event_id,
                    event.git_commit,
                    event.bindings,
                )
            previous = event.sha256
        return ExecutionState(
            run_id=self.manifest.run_id,
            feature_id=self.manifest.feature_id,
            status=run_status,
            last_sequence=len(events),
            last_sha256=previous,
            tasks=tuple(sorted(tasks.values(), key=lambda item: item.task_id)),
        )

    def reconstruct(self) -> ExecutionState:
        with self._lock():
            return self._reconstruct_unlocked()

    def _reconstruct_unlocked(self) -> ExecutionState:
        events = self._load_events_unlocked()
        return self._reconstruct_events(events)

    def write_checkpoint(self, extra: Mapping[str, object] | None = None) -> dict[str, object]:
        extra_dict = dict(extra or {})
        _validate_json_value(extra_dict, label="checkpoint extra")
        with self._lock():
            state = self._reconstruct_unlocked()
            body: dict[str, object] = {
                "apiVersion": "sdai.execution-checkpoint/v1",
                "run_id": self.manifest.run_id,
                "feature_id": self.manifest.feature_id,
                "last_sequence": state.last_sequence,
                "last_sha256": state.last_sha256,
                "state": state.as_dict(),
                "extra": extra_dict,
            }
            payload = dict(body)
            payload["sha256"] = _sha256_bytes(_canonical_bytes(body))
            _atomic_json(self.checkpoint_path, payload)
            return payload

    def load_checkpoint(self) -> dict[str, object] | None:
        with self._lock():
            if not self.checkpoint_path.exists():
                return None
            if self.checkpoint_path.is_symlink() or not self.checkpoint_path.is_file():
                raise _fail("SDAI-LEDGER-003", "checkpoint.json must be a regular non-symlink file")
            try:
                payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _fail("SDAI-LEDGER-004", f"invalid checkpoint.json: {exc}") from exc
            if not isinstance(payload, Mapping) or set(payload) != _CHECKPOINT_KEYS:
                raise _fail("SDAI-LEDGER-004", "invalid execution checkpoint fields")
            if payload.get("apiVersion") != "sdai.execution-checkpoint/v1":
                raise _fail("SDAI-LEDGER-004", "invalid execution checkpoint apiVersion")
            body = {key: payload[key] for key in _CHECKPOINT_KEYS if key != "sha256"}
            expected = _sha256_bytes(_canonical_bytes(body))
            if payload.get("sha256") != expected:
                raise _fail("SDAI-LEDGER-004", "checkpoint content hash mismatch")
            state = self._reconstruct_unlocked()
            if payload.get("run_id") != self.manifest.run_id or payload.get("feature_id") != self.manifest.feature_id:
                raise _fail("SDAI-LEDGER-004", "checkpoint run/feature identity mismatch")
            if payload.get("last_sequence") != state.last_sequence or payload.get("last_sha256") != state.last_sha256:
                raise _fail("SDAI-LEDGER-004", "checkpoint is stale relative to the current event ledger")
            if payload.get("state") != state.as_dict():
                raise _fail("SDAI-LEDGER-004", "checkpoint state does not match reconstructed ledger state")
            if not isinstance(payload.get("extra"), Mapping):
                raise _fail("SDAI-LEDGER-004", "checkpoint extra must be a mapping")
            return dict(payload)


def _manifest_from_mapping(raw: object) -> RunManifest:
    if not isinstance(raw, Mapping):
        raise _fail("SDAI-LEDGER-004", "run.json must contain a mapping")
    expected = {"apiVersion", "run_id", "feature_id", "workflow", "baseline_commit", "created_at"}
    if set(raw) != expected or raw.get("apiVersion") != "sdai.execution-run/v1":
        raise _fail("SDAI-LEDGER-004", "run.json fields do not match sdai.execution-run/v1")
    run_id = raw.get("run_id")
    workflow = raw.get("workflow")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise _fail("SDAI-LEDGER-004", "run.json run_id is invalid")
    if not isinstance(workflow, str) or not _WORKFLOW.fullmatch(workflow):
        raise _fail("SDAI-LEDGER-004", "run.json workflow is invalid")
    feature = validate_feature_id(str(raw.get("feature_id") or ""))
    baseline = _validate_commit(str(raw.get("baseline_commit") or ""), required=True)
    assert baseline is not None
    created_at = _validate_timestamp(str(raw.get("created_at") or ""))
    return RunManifest(run_id, feature, workflow, baseline, created_at)


def create_execution_run(
    project_root: Path,
    feature_id: str,
    workflow: str,
    baseline_commit: str,
    *,
    run_id: str | None = None,
) -> ExecutionLedger:
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    if not isinstance(workflow, str) or not _WORKFLOW.fullmatch(workflow):
        raise _fail("SDAI-LEDGER-001", f"invalid workflow name: {workflow!r}")
    baseline = _validate_commit(baseline_commit, required=True)
    assert baseline is not None
    feature_dir = _safe_existing_path(
        root,
        root / "specs" / feature,
        label="execution feature directory",
    )
    if not feature_dir.is_dir() or feature_dir.is_symlink():
        raise _fail(
            "SDAI-LEDGER-001",
            f"feature directory must already exist before creating a run: specs/{feature}",
        )
    created = _now()
    generated = run_id or f"run-{created.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:12]}"
    if not _RUN_ID.fullmatch(generated):
        raise _fail("SDAI-LEDGER-001", f"invalid run id: {generated!r}")
    run_dir = _safe_existing_path(
        root,
        feature_dir / ".sdai" / "execution" / generated,
        label="execution run directory",
    )
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise _fail("SDAI-LEDGER-001", f"execution run already exists: {generated}") from exc
    manifest = RunManifest(generated, feature, workflow, baseline, created.isoformat())
    manifest_path = run_dir / "run.json"
    try:
        _atomic_json(manifest_path, manifest.as_dict())
        ledger = ExecutionLedger(root, manifest)
        manifest_binding = ledger.binding_for_file(manifest_path, kind="evidence")
        ledger.append_event(
            "run.created",
            git_commit=baseline,
            bindings=(manifest_binding,),
            payload={"workflow": workflow, "manifest_sha256": manifest.sha256},
        )
        return ledger
    except Exception:
        # A partially-created run is never treated as valid because load_execution_run
        # requires both a valid manifest and a valid hash-chained run.created event.
        raise


def load_execution_run(project_root: Path, feature_id: str, run_id: str) -> ExecutionLedger:
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    if not _RUN_ID.fullmatch(run_id):
        raise _fail("SDAI-LEDGER-001", f"invalid run id: {run_id!r}")
    run_dir = _safe_existing_path(
        root,
        root / "specs" / feature / ".sdai" / "execution" / run_id,
        label="execution run directory",
    )
    manifest_path = run_dir / "run.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise _fail("SDAI-LEDGER-001", f"execution run manifest does not exist: {run_id}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("SDAI-LEDGER-004", f"invalid run.json: {exc}") from exc
    manifest = _manifest_from_mapping(payload)
    if manifest.feature_id != feature or manifest.run_id != run_id:
        raise _fail("SDAI-LEDGER-004", "run.json path identity does not match manifest identity")
    ledger = ExecutionLedger(root, manifest)
    events = ledger.load_events()
    if not events or events[0].kind != "run.created":
        raise _fail("SDAI-LEDGER-004", "execution run is missing its run.created ledger event")
    created = events[0]
    current_manifest_binding = ledger.binding_for_file(manifest_path, kind="evidence")
    if current_manifest_binding not in created.bindings:
        raise _fail("SDAI-LEDGER-004", "run.json byte identity does not match run.created evidence binding")
    if created.git_commit != manifest.baseline_commit:
        raise _fail("SDAI-LEDGER-004", "run.created Git baseline does not match run.json")
    if created.payload.get("workflow") != manifest.workflow:
        raise _fail("SDAI-LEDGER-004", "run.created workflow does not match run.json")
    if created.payload.get("manifest_sha256") != manifest.sha256:
        raise _fail("SDAI-LEDGER-004", "run.created manifest hash does not match run.json")
    return ledger
