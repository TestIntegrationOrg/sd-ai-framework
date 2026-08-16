from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import re
import stat
import subprocess
from typing import Mapping
import unicodedata

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from sdai.models import validate_feature_id


PR_EVIDENCE_API_VERSION = "sdai.pr-evidence/v1"
PR_EVIDENCE_FILENAME = "pr-evidence.yaml"
PR_EVIDENCE_MAX_BYTES = 1024 * 1024
PR_EVIDENCE_MAX_REFERENCES = 1024
PR_EVIDENCE_MAX_LINKS = 10_000


class PullRequestEvidenceError(RuntimeError):
    """Raised when repository-local PR evidence is unsafe or invalid."""


class PullRequestState(StrEnum):
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


_REPOSITORY_ID = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_REFERENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_ALLOWED_LINK_PREFIXES = ("task:", "code:", "test:", "evidence:")
_TOP_KEYS = frozenset({"apiVersion", "kind", "featureId", "repositoryId", "pullRequests"})
_PR_REQUIRED = frozenset({"id", "headCommit", "state", "links"})
_PR_ALLOWED = _PR_REQUIRED | {"provider"}
_PROVIDER_ALLOWED = frozenset({"name", "reference", "url"})


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate mapping key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _fail(code: str, message: str) -> PullRequestEvidenceError:
    return PullRequestEvidenceError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-PR-EVIDENCE-001", "PR evidence must be canonical finite JSON") from exc


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-PR-EVIDENCE-001", f"{label} must be a string-keyed mapping")
    return value


def _keys(
    raw: Mapping[str, object],
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    label: str,
) -> None:
    missing = sorted(required - set(raw))
    unknown = sorted(set(raw) - allowed)
    if missing:
        raise _fail(
            "SDAI-PR-EVIDENCE-001",
            f"{label} is missing required field(s): {', '.join(missing)}",
        )
    if unknown:
        raise _fail(
            "SDAI-PR-EVIDENCE-001",
            f"{label} contains unsupported field(s): {', '.join(unknown)}",
        )


def _text(value: object, *, label: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail("SDAI-PR-EVIDENCE-001", f"{label} must be non-empty normalized text")
    candidate = unicodedata.normalize("NFC", value)
    if candidate != value or len(candidate) > maximum or any(ord(character) < 32 for character in candidate):
        raise _fail("SDAI-PR-EVIDENCE-001", f"{label} is not normalized portable text")
    return candidate


def _repository_id(value: object) -> str:
    candidate = _text(value, label="repositoryId", maximum=128)
    if not _REPOSITORY_ID.fullmatch(candidate):
        raise _fail("SDAI-PR-EVIDENCE-001", "repositoryId must be a portable lowercase identifier")
    return candidate


def _reference_id(value: object) -> str:
    candidate = _text(value, label="pull request id", maximum=128)
    if not _REFERENCE_ID.fullmatch(candidate):
        raise _fail("SDAI-PR-EVIDENCE-001", "pull request id must be a portable local identifier")
    return candidate


def _commit(value: object) -> str:
    candidate = _text(value, label="headCommit", maximum=64)
    if not _COMMIT.fullmatch(candidate):
        raise _fail("SDAI-PR-EVIDENCE-001", "headCommit must be a lowercase 40- or 64-hex Git object id")
    return candidate


def _link(value: object) -> str:
    candidate = _text(value, label="PR trace link", maximum=512)
    if not candidate.startswith(_ALLOWED_LINK_PREFIXES):
        raise _fail(
            "SDAI-PR-EVIDENCE-001",
            "PR links may reference only task, code, test, or evidence trace nodes",
        )
    return candidate


def _redirect(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _fail("SDAI-PR-EVIDENCE-002", "PR evidence redirect status could not be verified") from exc


def _evidence_path(repository_root: Path, feature_id: str) -> Path:
    feature = validate_feature_id(feature_id)
    candidate = repository_root / "specs" / "changes" / feature / PR_EVIDENCE_FILENAME
    current = Path(candidate.anchor)
    for part in candidate.absolute().parts[1:]:
        current = current / part
        if current.exists() and _redirect(current):
            raise _fail(
                "SDAI-PR-EVIDENCE-002",
                "PR evidence path must not contain symlinks, junctions, or reparse points",
            )
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repository_root.resolve(strict=True))
    except FileNotFoundError:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise _fail("SDAI-PR-EVIDENCE-002", "PR evidence must remain inside the repository") from exc
    if not resolved.is_file():
        raise _fail("SDAI-PR-EVIDENCE-002", "PR evidence must be a regular file")
    return resolved


def _read_bounded(path: Path) -> tuple[bytes, str]:
    try:
        with path.open("rb") as stream:
            content = stream.read(PR_EVIDENCE_MAX_BYTES + 1)
    except OSError as exc:
        raise _fail("SDAI-PR-EVIDENCE-002", "unable to read PR evidence") from exc
    if len(content) > PR_EVIDENCE_MAX_BYTES:
        raise _fail("SDAI-PR-EVIDENCE-001", "PR evidence exceeds the 1 MiB input limit")
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail("SDAI-PR-EVIDENCE-001", "PR evidence must be valid UTF-8") from exc
    return content, text.replace("\r\n", "\n").replace("\r", "\n")


def _run_git(repository_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repository_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
    except OSError as exc:
        raise _fail("SDAI-PR-EVIDENCE-003", "local Git command could not be started") from exc


def _commit_status(repository_root: Path, commit: str) -> tuple[bool, bool, str | None]:
    exists = _run_git(repository_root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    if exists.returncode != 0:
        return False, False, None
    resolved = _run_git(repository_root, ["rev-parse", "--verify", f"{commit}^{{commit}}"])
    if resolved.returncode != 0:
        return False, False, None
    full = resolved.stdout.strip().lower()
    reachable = _run_git(repository_root, ["merge-base", "--is-ancestor", full, "HEAD"])
    if reachable.returncode not in {0, 1}:
        raise _fail("SDAI-PR-EVIDENCE-003", "local Git reachability check failed")
    return True, reachable.returncode == 0, full


@dataclass(frozen=True)
class PullRequestProviderMetadata:
    name: str | None = None
    reference: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if self.name is not None:
            object.__setattr__(self, "name", _text(self.name, label="provider name", maximum=128))
        if self.reference is not None:
            object.__setattr__(self, "reference", _text(self.reference, label="provider reference", maximum=256))
        if self.url is not None:
            object.__setattr__(self, "url", _text(self.url, label="provider url", maximum=2048))
        if self.name is None and self.reference is None and self.url is None:
            raise _fail("SDAI-PR-EVIDENCE-001", "provider metadata must contain at least one field")

    def as_dict(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self.name is not None:
            payload["name"] = self.name
        if self.reference is not None:
            payload["reference"] = self.reference
        if self.url is not None:
            payload["url"] = self.url
        return payload

    @classmethod
    def from_dict(cls, value: object) -> "PullRequestProviderMetadata":
        raw = _mapping(value, label="provider metadata")
        _keys(raw, required=frozenset(), allowed=_PROVIDER_ALLOWED, label="provider metadata")
        if not raw:
            raise _fail("SDAI-PR-EVIDENCE-001", "provider metadata must not be empty")
        return cls(
            name=raw.get("name"),  # type: ignore[arg-type]
            reference=raw.get("reference"),  # type: ignore[arg-type]
            url=raw.get("url"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class PullRequestReference:
    id: str
    head_commit: str
    state: PullRequestState
    links: tuple[str, ...]
    provider: PullRequestProviderMetadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _reference_id(self.id))
        object.__setattr__(self, "head_commit", _commit(self.head_commit))
        try:
            state = self.state if isinstance(self.state, PullRequestState) else PullRequestState(self.state)
        except ValueError as exc:
            raise _fail("SDAI-PR-EVIDENCE-001", f"unsupported PR state: {self.state!r}") from exc
        object.__setattr__(self, "state", state)
        if not isinstance(self.links, (tuple, list)) or not self.links:
            raise _fail("SDAI-PR-EVIDENCE-001", "PR evidence requires at least one trace-node link")
        if len(self.links) > PR_EVIDENCE_MAX_LINKS:
            raise _fail("SDAI-PR-EVIDENCE-001", "PR evidence contains too many links")
        links = tuple(sorted(_link(item) for item in self.links))
        if len(set(links)) != len(links):
            raise _fail("SDAI-PR-EVIDENCE-004", f"PR reference '{self.id}' contains duplicate trace links")
        object.__setattr__(self, "links", links)
        if self.provider is not None and not isinstance(self.provider, PullRequestProviderMetadata):
            raise _fail("SDAI-PR-EVIDENCE-001", "provider metadata is invalid")

    @property
    def provider_identity_hint(self) -> tuple[str, str] | None:
        if self.provider is None or self.provider.name is None or self.provider.reference is None:
            return None
        return (self.provider.name.casefold(), self.provider.reference.casefold())

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "headCommit": self.head_commit,
            "id": self.id,
            "links": list(self.links),
            "state": self.state.value,
        }
        if self.provider is not None:
            payload["provider"] = self.provider.as_dict()
        return payload

    @classmethod
    def from_dict(cls, value: object) -> "PullRequestReference":
        raw = _mapping(value, label="pull request reference")
        _keys(raw, required=_PR_REQUIRED, allowed=_PR_ALLOWED, label="pull request reference")
        links = raw["links"]
        if not isinstance(links, list):
            raise _fail("SDAI-PR-EVIDENCE-001", "pull request links must be a list")
        provider = raw.get("provider")
        return cls(
            id=raw["id"],  # type: ignore[arg-type]
            head_commit=raw["headCommit"],  # type: ignore[arg-type]
            state=raw["state"],  # type: ignore[arg-type]
            links=tuple(links),
            provider=None if provider is None else PullRequestProviderMetadata.from_dict(provider),
        )


@dataclass(frozen=True)
class PullRequestEvidenceManifest:
    feature_id: str
    repository_id: str
    pull_requests: tuple[PullRequestReference, ...]
    source: str
    source_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", validate_feature_id(self.feature_id))
        object.__setattr__(self, "repository_id", _repository_id(self.repository_id))
        if not isinstance(self.pull_requests, (tuple, list)) or not self.pull_requests:
            raise _fail("SDAI-PR-EVIDENCE-001", "pullRequests must be a non-empty list")
        if len(self.pull_requests) > PR_EVIDENCE_MAX_REFERENCES:
            raise _fail("SDAI-PR-EVIDENCE-001", "PR evidence contains too many references")
        if not all(isinstance(item, PullRequestReference) for item in self.pull_requests):
            raise _fail("SDAI-PR-EVIDENCE-001", "pullRequests contains an invalid reference")
        ordered = tuple(sorted(self.pull_requests, key=lambda item: item.id))
        ids = [item.id for item in ordered]
        if len(ids) != len(set(ids)):
            raise _fail("SDAI-PR-EVIDENCE-004", "pull request local ids must be unique")
        object.__setattr__(self, "pull_requests", ordered)
        object.__setattr__(self, "source", _text(self.source, label="PR evidence source", maximum=512))
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.source_sha256):
            raise _fail("SDAI-PR-EVIDENCE-001", "source_sha256 must be a lowercase SHA-256")

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PR_EVIDENCE_API_VERSION,
            "featureId": self.feature_id,
            "kind": "PullRequestEvidence",
            "pullRequests": [item.as_dict() for item in self.pull_requests],
            "repositoryId": self.repository_id,
        }

    @property
    def sha256(self) -> str:
        return _sha256_json(self.as_dict())

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        source: str,
        source_sha256: str,
    ) -> "PullRequestEvidenceManifest":
        raw = _mapping(value, label="PR evidence manifest")
        _keys(raw, required=_TOP_KEYS, allowed=_TOP_KEYS, label="PR evidence manifest")
        if raw["apiVersion"] != PR_EVIDENCE_API_VERSION:
            raise _fail("SDAI-PR-EVIDENCE-001", "unsupported PR evidence apiVersion")
        if raw["kind"] != "PullRequestEvidence":
            raise _fail("SDAI-PR-EVIDENCE-001", "PR evidence kind must be 'PullRequestEvidence'")
        pull_requests = raw["pullRequests"]
        if not isinstance(pull_requests, list):
            raise _fail("SDAI-PR-EVIDENCE-001", "pullRequests must be a list")
        return cls(
            feature_id=raw["featureId"],  # type: ignore[arg-type]
            repository_id=raw["repositoryId"],  # type: ignore[arg-type]
            pull_requests=tuple(PullRequestReference.from_dict(item) for item in pull_requests),
            source=source,
            source_sha256=source_sha256,
        )


@dataclass(frozen=True)
class ResolvedPullRequestReference:
    reference: PullRequestReference
    commit_exists: bool
    commit_reachable: bool
    resolved_commit: str | None

    @property
    def current(self) -> bool:
        return self.commit_exists and self.commit_reachable and self.reference.state is not PullRequestState.CLOSED

    @property
    def satisfies_traceability(self) -> bool:
        return self.current

    def as_dict(self) -> dict[str, object]:
        return {
            "commitExists": self.commit_exists,
            "commitReachable": self.commit_reachable,
            "current": self.current,
            "reference": self.reference.as_dict(),
            "resolvedCommit": self.resolved_commit,
            "satisfiesTraceability": self.satisfies_traceability,
        }


@dataclass(frozen=True)
class ResolvedPullRequestEvidence:
    manifest: PullRequestEvidenceManifest
    references: tuple[ResolvedPullRequestReference, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": "sdai.resolved-pr-evidence/v1",
            "manifestSha256": self.manifest.sha256,
            "references": [item.as_dict() for item in self.references],
            "source": self.manifest.source,
            "sourceSha256": self.manifest.source_sha256,
        }

    @property
    def sha256(self) -> str:
        return _sha256_json(self.as_dict())


def load_pull_request_evidence(
    repository_root: Path,
    feature_id: str,
    repository_id: str,
) -> PullRequestEvidenceManifest | None:
    root = Path(repository_root).resolve(strict=True)
    feature = validate_feature_id(feature_id)
    try:
        path = _evidence_path(root, feature)
    except FileNotFoundError:
        return None
    content, text = _read_bounded(path)
    try:
        if any(isinstance(event, yaml.events.AliasEvent) for event in yaml.parse(text)):
            raise _fail("SDAI-PR-EVIDENCE-001", "PR evidence YAML aliases are not allowed")
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except PullRequestEvidenceError:
        raise
    except (OverflowError, RecursionError, ValueError, yaml.YAMLError) as exc:
        raise _fail("SDAI-PR-EVIDENCE-001", "PR evidence YAML is malformed") from exc
    source = path.relative_to(root).as_posix()
    manifest = PullRequestEvidenceManifest.from_dict(
        raw,
        source=source,
        source_sha256=_sha256_bytes(content),
    )
    if manifest.feature_id != feature:
        raise _fail(
            "SDAI-PR-EVIDENCE-005",
            "PR evidence featureId does not match its feature directory",
        )
    expected_repository = _repository_id(repository_id)
    if manifest.repository_id != expected_repository:
        raise _fail(
            "SDAI-PR-EVIDENCE-005",
            "PR evidence repositoryId does not match the declared repository participant",
        )
    return manifest


def resolve_pull_request_evidence(
    repository_root: Path,
    feature_id: str,
    repository_id: str,
) -> ResolvedPullRequestEvidence | None:
    root = Path(repository_root).resolve(strict=True)
    manifest = load_pull_request_evidence(root, feature_id, repository_id)
    if manifest is None:
        return None
    resolved: list[ResolvedPullRequestReference] = []
    for reference in manifest.pull_requests:
        exists, reachable, full = _commit_status(root, reference.head_commit)
        resolved.append(ResolvedPullRequestReference(reference, exists, reachable, full))
    return ResolvedPullRequestEvidence(manifest, tuple(resolved))


__all__ = [
    "PR_EVIDENCE_API_VERSION",
    "PR_EVIDENCE_FILENAME",
    "PullRequestEvidenceError",
    "PullRequestEvidenceManifest",
    "PullRequestProviderMetadata",
    "PullRequestReference",
    "PullRequestState",
    "ResolvedPullRequestEvidence",
    "ResolvedPullRequestReference",
    "load_pull_request_evidence",
    "resolve_pull_request_evidence",
]
