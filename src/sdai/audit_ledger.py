from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Iterable, Mapping

from sdai.audit_contracts import (
    AUDIT_MAX_EVENT_BYTES,
    AUDIT_MAX_EVENTS,
    AUDIT_MAX_LEDGER_BYTES,
    _ZERO_HASH,
    _canonical_bytes,
    _fail,
    _feature_id,
    _feature_workspace,
    _safe_component_chain,
    _sha256_bytes,
)
from sdai.audit_provenance import (
    AuditAction,
    AuditActor,
    AuditBinding,
    AuditEvent,
    AuditExecution,
    AuditLedgerSnapshot,
)


_LOCK_ANCHOR = b"SDAI-AUDIT-LOCK-v1\n"
_BINARY_FLAG = getattr(os, "O_BINARY", 0)
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve(strict=False))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise _fail("SDAI-AUDIT-006", "operating system returned a short/zero audit write")
        view = view[written:]


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(directory, flags)
    except OSError as exc:
        raise _fail("SDAI-AUDIT-006", "unable to open audit directory for durability sync") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise _fail("SDAI-AUDIT-006", "unable to sync audit directory") from exc
    finally:
        os.close(fd)


class _AuditLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None
        self._windows = os.name == "nt"
        self._thread_lock: threading.Lock | None = None

    def _release_thread_lock(self) -> None:
        if self._thread_lock is None:
            return
        self._thread_lock.release()
        self._thread_lock = None

    def __enter__(self) -> "_AuditLock":
        # Windows byte-range locking can report EDEADLK when sibling threads in the
        # same process contend on separate descriptors for the same file. Serialize
        # those writers locally first; the OS lock still provides cross-process
        # exclusion and therefore remains part of the durability/integrity boundary.
        self._thread_lock = _thread_lock_for(self.path)
        self._thread_lock.acquire()
        try:
            if self.path.is_symlink():
                raise _fail("SDAI-AUDIT-004", "audit ledger lock must not be a symlink")
            flags = os.O_CREAT | os.O_RDWR | _BINARY_FLAG
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(self.path, flags, 0o600)
            except OSError as exc:
                raise _fail("SDAI-AUDIT-006", "unable to open audit ledger lock") from exc
            try:
                if os.fstat(fd).st_size == 0:
                    os.lseek(fd, 0, os.SEEK_SET)
                    _write_all(fd, _LOCK_ANCHOR)
                    os.fsync(fd)
                self._acquire(fd)
                self.fd = fd
                return self
            except Exception:
                os.close(fd)
                raise
        except Exception:
            self._release_thread_lock()
            raise

    def _acquire(self, fd: int) -> None:
        try:
            if self._windows:
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                # Sibling threads have already been serialized above. LK_LOCK now
                # handles only cross-process contention on the canonical lock byte.
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
        except (BlockingIOError, OSError) as exc:
            raise _fail("SDAI-AUDIT-006", "unable to acquire audit ledger lock") from exc

    def _release(self, fd: int) -> None:
        try:
            if self._windows:
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.fd is not None:
                self._release(self.fd)
                os.close(self.fd)
                self.fd = None
        finally:
            self._release_thread_lock()


class AuditLedger:
    """Feature-scoped append-only audit/provenance ledger.

    This ledger records evidence/provenance only. It does not participate in workflow
    state transitions, approvals, or promotion authority.
    """

    def __init__(self, project_root: Path, feature_id: str) -> None:
        self.project_root = project_root.resolve()
        self.feature_id = _feature_id(feature_id)
        self.feature_workspace = _feature_workspace(self.project_root, self.feature_id)
        self.audit_dir = _safe_component_chain(
            self.project_root,
            self.feature_workspace / ".sdai" / "audit",
            label="audit ledger directory",
        )
        self.events_path = self.audit_dir / "events.jsonl"
        self.lock_path = self.audit_dir / "ledger.lock"
        self._ensure_directory()

    def _ensure_directory(self) -> None:
        current = self.feature_workspace
        for part in (".sdai", "audit"):
            current = current / part
            if current.is_symlink():
                raise _fail("SDAI-AUDIT-004", "audit ledger directory contains a symlink component")
            if current.exists() and not current.is_dir():
                raise _fail("SDAI-AUDIT-004", "audit ledger path component is not a directory")
            current.mkdir(exist_ok=True)
        _fsync_directory(self.audit_dir.parent)

    def _lock(self) -> _AuditLock:
        _safe_component_chain(self.project_root, self.audit_dir, label="audit ledger directory")
        return _AuditLock(self.lock_path)

    def _bounded_bytes(self) -> bytes:
        if self.events_path.is_symlink():
            raise _fail("SDAI-AUDIT-004", "audit events file must not be a symlink")
        if not self.events_path.exists():
            return b""
        if not self.events_path.is_file():
            raise _fail("SDAI-AUDIT-004", "audit events path must be a regular file")
        try:
            with self.events_path.open("rb") as stream:
                content = stream.read(AUDIT_MAX_LEDGER_BYTES + 1)
        except OSError as exc:
            raise _fail("SDAI-AUDIT-006", "unable to read audit events") from exc
        if len(content) > AUDIT_MAX_LEDGER_BYTES:
            raise _fail("SDAI-AUDIT-005", "audit ledger exceeds the size limit")
        return content

    def _parse_bytes(self, content: bytes, *, recover_tail: bool) -> tuple[AuditEvent, ...]:
        if not content:
            return ()
        working = content
        if not working.endswith(b"\n"):
            boundary = working.rfind(b"\n")
            tail = working[boundary + 1 :]
            try:
                json.loads(tail.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if not recover_tail:
                    raise _fail("SDAI-AUDIT-005", "audit ledger contains an incomplete final record")
                working = working[: boundary + 1] if boundary >= 0 else b""
                self._truncate(len(working))
            else:
                raise _fail(
                    "SDAI-AUDIT-005",
                    "audit ledger final record is complete JSON but missing the canonical newline",
                )
        events: list[AuditEvent] = []
        previous = _ZERO_HASH
        # Split on the canonical LF byte only. bytes.splitlines() would silently
        # discard CR from CRLF and could make non-canonical ledger bytes appear valid.
        for index, raw_line in enumerate(working[:-1].split(b"\n"), start=1):
            if not raw_line:
                raise _fail("SDAI-AUDIT-005", f"audit ledger line {index} is empty")
            if len(raw_line) > AUDIT_MAX_EVENT_BYTES:
                raise _fail("SDAI-AUDIT-005", f"audit ledger line {index} exceeds the event size limit")
            try:
                raw = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _fail("SDAI-AUDIT-005", f"audit ledger line {index} is invalid JSON") from exc
            event = AuditEvent.from_mapping(raw)
            canonical = _canonical_bytes(event.to_dict())
            if raw_line != canonical:
                raise _fail("SDAI-AUDIT-005", f"audit ledger line {index} is not canonical JSON")
            if event.feature_id != self.feature_id:
                raise _fail("SDAI-AUDIT-005", f"audit ledger line {index} belongs to another feature")
            if event.sequence != index:
                raise _fail("SDAI-AUDIT-005", f"audit ledger sequence gap at line {index}")
            if event.previous_sha256 != previous:
                raise _fail("SDAI-AUDIT-005", f"audit ledger chain mismatch at line {index}")
            previous = event.sha256
            events.append(event)
            if len(events) > AUDIT_MAX_EVENTS:
                raise _fail("SDAI-AUDIT-005", "audit ledger exceeds the event count limit")
        return tuple(events)

    def _truncate(self, size: int) -> None:
        if self.events_path.is_symlink():
            raise _fail("SDAI-AUDIT-004", "audit events file must not be a symlink")
        try:
            fd = os.open(self.events_path, os.O_WRONLY | _BINARY_FLAG)
        except OSError as exc:
            raise _fail("SDAI-AUDIT-006", "unable to open audit ledger for crash-tail recovery") from exc
        try:
            os.ftruncate(fd, size)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self.audit_dir)

    def _read_locked(self, *, recover_tail: bool) -> tuple[AuditEvent, ...]:
        return self._parse_bytes(self._bounded_bytes(), recover_tail=recover_tail)

    def read(self) -> tuple[AuditEvent, ...]:
        with self._lock():
            return self._read_locked(recover_tail=False)

    def verify(self) -> AuditLedgerSnapshot:
        with self._lock():
            events = self._read_locked(recover_tail=False)
            export = b"".join(_canonical_bytes(item.to_dict()) + b"\n" for item in events)
            return AuditLedgerSnapshot(
                feature_id=self.feature_id,
                event_count=len(events),
                head_sha256=events[-1].sha256 if events else _ZERO_HASH,
                export_sha256=_sha256_bytes(export),
            )

    def export_jsonl(self) -> bytes:
        with self._lock():
            events = self._read_locked(recover_tail=False)
            return b"".join(_canonical_bytes(item.to_dict()) + b"\n" for item in events)

    def append(
        self,
        *,
        category: str,
        actor: AuditActor,
        action: AuditAction,
        execution: AuditExecution | None = None,
        bindings: Iterable[AuditBinding] = (),
        metadata: Mapping[str, object] | None = None,
        occurred_at: str | None = None,
    ) -> AuditEvent:
        with self._lock():
            events = self._read_locked(recover_tail=True)
            if len(events) >= AUDIT_MAX_EVENTS:
                raise _fail("SDAI-AUDIT-005", "audit ledger reached the event count limit")
            sequence = len(events) + 1
            previous = events[-1].sha256 if events else _ZERO_HASH
            event = AuditEvent.create(
                sequence=sequence,
                feature_id=self.feature_id,
                category=category,
                occurred_at=occurred_at
                or datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                actor=actor,
                action=action,
                execution=execution,
                bindings=bindings,
                metadata=metadata,
                previous_sha256=previous,
            )
            encoded = _canonical_bytes(event.to_dict()) + b"\n"
            if len(encoded) > AUDIT_MAX_EVENT_BYTES:
                raise _fail("SDAI-AUDIT-002", "audit event exceeds the event size limit")
            current_size = self.events_path.stat().st_size if self.events_path.exists() else 0
            if current_size + len(encoded) > AUDIT_MAX_LEDGER_BYTES:
                raise _fail("SDAI-AUDIT-005", "audit ledger would exceed the size limit")
            if self.events_path.is_symlink():
                raise _fail("SDAI-AUDIT-004", "audit events file must not be a symlink")
            flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND | _BINARY_FLAG
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(self.events_path, flags, 0o600)
            except OSError as exc:
                raise _fail("SDAI-AUDIT-006", "unable to open audit events for append") from exc
            try:
                _write_all(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_directory(self.audit_dir)
            return event


__all__ = ["AuditLedger"]
