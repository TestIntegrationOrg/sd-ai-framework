from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Iterable, Mapping

import yaml

from sdai.architecture_drift import (
    ApprovedArchitecture,
    ArchitectureDriftError,
    ArchitectureFactKind,
    ArchitectureObservation,
    ObservedArchitectureFact,
)
from sdai.architecture_repository import ArchitectureRepositoryIndex, reject_repository_symlink_chain
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.structured_contracts import UniqueKeySafeLoader, normalize_structured_json
from sdai.trace_graph import TraceProvenance


DEPLOYMENT_OBSERVER_ID = "repository-deployments"
DEPLOYMENT_SOURCES_API_VERSION = "sdai.deployment-sources/v1"
DEPLOYMENT_SOURCES_PATH = ".sdai/deployments.yaml"
DEPLOYMENT_MANIFEST_MAX_BYTES = 1024 * 1024
DEPLOYMENT_SOURCE_MAX_BYTES = 8 * 1024 * 1024
DEPLOYMENT_MAX_SOURCES = 1024
DEPLOYMENT_MAX_DOCUMENTS = 10_000
DEPLOYMENT_MAX_FACTS = 100_000
DEPLOYMENT_MAX_PORTS = 256

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}$")
_ENVIRONMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_PORT_PROTOCOLS = frozenset({"TCP", "UDP", "SCTP", "HTTP", "HTTPS"})
_K8S_WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"})
_SUPPORTED_SOURCE_KINDS = frozenset({"kubernetes", "compose", "terraform"})
_EXTERNAL_SUBJECT = "external:public"


@dataclass(frozen=True, slots=True)
class DeploymentSource:
    source_id: str
    kind: str
    path: str
    environment: str


@dataclass(frozen=True, slots=True)
class _Placement:
    component: str
    target: str
    environment: str
    namespace: str
    provenance: tuple[TraceProvenance, ...]


@dataclass(frozen=True, slots=True)
class _TerraformResource:
    resource_type: str
    name: str
    block: str
    line: int
    reference: str


def _fail(code: str, message: str) -> ArchitectureDriftError:
    return ArchitectureDriftError(f"{code}: {message}")


def _safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None or "\\" in value:
        raise _fail("SDAI-ARCH-DEPLOY-002", f"{label} must be a safe portable identifier")
    return value


def _safe_environment(value: object) -> str:
    if not isinstance(value, str) or _ENVIRONMENT.fullmatch(value) is None:
        raise _fail("SDAI-ARCH-DEPLOY-002", "deployment source environment must be a safe portable identifier")
    return value


def _safe_name(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise _fail("SDAI-ARCH-DEPLOY-005", f"{label} must be bounded portable text")
    return value


def _portable_path(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value or "\x00" in value:
        raise _fail("SDAI-ARCH-DEPLOY-002", "deployment source path must be repository-relative POSIX text")
    if _DRIVE.match(value):
        raise _fail("SDAI-ARCH-DEPLOY-002", "deployment source path must not be a Windows drive path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise _fail("SDAI-ARCH-DEPLOY-002", "deployment source path is unsafe")
    if len(value.encode("utf-8")) > 4096 or len(path.parts) > 64:
        raise _fail("SDAI-ARCH-DEPLOY-002", "deployment source path exceeds portable limits")
    return path.as_posix()


def _read_bytes(root: Path, relative: str, *, maximum: int, label: str) -> bytes:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        safe = ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-ARCH-DEPLOY-003", f"{label} escapes the project workspace") from exc
    if not safe.exists():
        raise _fail("SDAI-ARCH-DEPLOY-003", f"{label} does not exist: {relative}")
    try:
        reject_repository_symlink_chain(root, safe, label=label)
    except ArchitectureDriftError as exc:
        raise _fail("SDAI-ARCH-DEPLOY-003", f"{label} is unsafe: {relative}") from exc
    if safe.is_symlink() or not safe.is_file():
        raise _fail("SDAI-ARCH-DEPLOY-003", f"{label} must be a regular non-symlink file: {relative}")
    try:
        with safe.open("rb") as stream:
            data = stream.read(maximum + 1)
    except OSError as exc:
        raise _fail("SDAI-ARCH-DEPLOY-003", f"unable to read {label}: {relative}") from exc
    if len(data) > maximum:
        raise _fail("SDAI-ARCH-DEPLOY-003", f"{label} exceeds the {maximum}-byte limit: {relative}")
    return data


def _decode(data: bytes, *, label: str) -> str:
    try:
        return data.decode("utf-8-sig", errors="strict").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise _fail("SDAI-ARCH-DEPLOY-003", f"{label} must be valid UTF-8") from exc


def load_deployment_sources(project_root: Path) -> tuple[DeploymentSource, ...]:
    root = project_root.resolve()
    manifest = root / DEPLOYMENT_SOURCES_PATH
    if not manifest.exists():
        return ()
    data = _read_bytes(root, DEPLOYMENT_SOURCES_PATH, maximum=DEPLOYMENT_MANIFEST_MAX_BYTES, label="deployment source manifest")
    text = _decode(data, label="deployment source manifest")
    try:
        raw = yaml.load(text, Loader=UniqueKeySafeLoader)
        normalized = normalize_structured_json(raw, max_nodes=50_000, max_depth=32)
    except (yaml.YAMLError, ValueError) as exc:
        raise _fail("SDAI-ARCH-DEPLOY-002", "deployment source manifest is not bounded unique-key YAML") from exc
    if not isinstance(normalized, Mapping) or set(normalized) != {"apiVersion", "kind", "sources"}:
        raise _fail("SDAI-ARCH-DEPLOY-002", "deployment source manifest fields must be exactly apiVersion, kind, sources")
    if normalized.get("apiVersion") != DEPLOYMENT_SOURCES_API_VERSION or normalized.get("kind") != "DeploymentSources":
        raise _fail("SDAI-ARCH-DEPLOY-002", "unsupported deployment source manifest API version/kind")
    sources_raw = normalized.get("sources")
    if not isinstance(sources_raw, list) or len(sources_raw) > DEPLOYMENT_MAX_SOURCES:
        raise _fail("SDAI-ARCH-DEPLOY-002", "deployment source manifest sources must be a bounded list")
    sources: list[DeploymentSource] = []
    ids: set[str] = set()
    paths: dict[str, DeploymentSource] = {}
    for item in sources_raw:
        if not isinstance(item, Mapping) or set(item) != {"id", "kind", "path", "environment"}:
            raise _fail("SDAI-ARCH-DEPLOY-002", "deployment source fields must be exactly id, kind, path, environment")
        source_id = _safe_id(item.get("id"), label="deployment source id")
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in _SUPPORTED_SOURCE_KINDS:
            raise _fail("SDAI-ARCH-DEPLOY-002", f"deployment source {source_id!r} has unsupported kind")
        path = _portable_path(item.get("path"))
        environment = _safe_environment(item.get("environment"))
        source = DeploymentSource(source_id, kind, path, environment)
        if source_id in ids:
            raise _fail("SDAI-ARCH-DEPLOY-002", f"duplicate deployment source id: {source_id}")
        previous = paths.get(path)
        if previous is not None and previous != source:
            raise _fail("SDAI-ARCH-DEPLOY-002", f"deployment source path {path!r} has conflicting declarations")
        ids.add(source_id)
        paths[path] = source
        sources.append(source)
    return tuple(sorted(sources, key=lambda item: (item.source_id, item.kind, item.path, item.environment)))


def _slug(value: str, *, limit: int = 48) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._") or "resource"
    if len(normalized) <= limit:
        return normalized
    digest = sha256(value.encode("utf-8")).hexdigest()[:12]
    return normalized[: max(1, limit - 13)] + "-" + digest


def deployment_subject_id(source_id: str, platform: str, namespace: str, kind: str, name: str) -> str:
    parts = (
        "workload", _slug(source_id, limit=32), _slug(platform, limit=16),
        _slug(namespace, limit=40), _slug(kind.casefold(), limit=28), _slug(name, limit=48),
    )
    value = ":".join(parts)
    if len(value) <= 240:
        return value
    digest = sha256(value.encode("utf-8")).hexdigest()[:20]
    return ":".join(parts[:3]) + ":sha256:" + digest


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _provenance(source_path: str, line: int, detail: str) -> tuple[TraceProvenance, ...]:
    return (TraceProvenance(source_path, max(1, line), detail=detail),)


def _component_candidates(
    repository: ArchitectureRepositoryIndex, source_path: str, *, name: str, explicit: Iterable[str | None]
) -> str:
    components = set(repository.component_ids)
    candidates: set[str] = set()
    explicit_values = {value for value in explicit if value is not None}
    if len(explicit_values) > 1:
        raise _fail("SDAI-ARCH-DEPLOY-004", f"deployment resource {name!r} has conflicting explicit component mappings")
    if explicit_values:
        component = next(iter(explicit_values))
        if component not in components:
            raise _fail("SDAI-ARCH-DEPLOY-004", f"deployment resource {name!r} maps to unknown component {component!r}")
        candidates.add(component)
    path_owner = repository.owner_for_relative_path(PurePosixPath(source_path))
    if path_owner is not None:
        candidates.add(path_owner)
    if name in components:
        candidates.add(name)
    if len(candidates) != 1:
        if not candidates:
            raise _fail("SDAI-ARCH-DEPLOY-004", f"deployment resource {name!r} has no deterministic component mapping")
        raise _fail("SDAI-ARCH-DEPLOY-004", f"deployment resource {name!r} has ambiguous component mapping: {', '.join(sorted(candidates))}")
    return next(iter(candidates))


def _ports(values: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for raw in values:
        port = raw.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise _fail("SDAI-ARCH-DEPLOY-005", "deployment port must be an integer from 1 to 65535")
        protocol = raw.get("protocol", "TCP")
        if not isinstance(protocol, str) or protocol.upper() not in _PORT_PROTOCOLS:
            raise _fail("SDAI-ARCH-DEPLOY-005", "deployment port protocol is unsupported")
        item: dict[str, object] = {"port": port, "protocol": protocol.upper()}
        target = raw.get("targetPort")
        if target is not None:
            if isinstance(target, int) and not isinstance(target, bool) and 1 <= target <= 65535:
                item["targetPort"] = target
            elif isinstance(target, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,63}", target):
                item["targetPort"] = target
            else:
                raise _fail("SDAI-ARCH-DEPLOY-005", "deployment targetPort is invalid")
        published = raw.get("published")
        if published is not None:
            if not isinstance(published, int) or isinstance(published, bool) or not 1 <= published <= 65535:
                raise _fail("SDAI-ARCH-DEPLOY-005", "published deployment port is invalid")
            item["published"] = published
        result.append(item)
        if len(result) > DEPLOYMENT_MAX_PORTS:
            raise _fail("SDAI-ARCH-DEPLOY-005", "deployment resource exceeds the port limit")
    unique = {json.dumps(item, sort_keys=True, separators=(",", ":")): item for item in result}
    return [unique[key] for key in sorted(unique)]


def _fact(*, source: str, target: str, attributes: Mapping[str, object], provenance: tuple[TraceProvenance, ...]) -> ObservedArchitectureFact:
    return ObservedArchitectureFact(kind=ArchitectureFactKind.DEPLOYMENT, source=source, target=target, attributes=attributes, provenance=provenance)


def _exposure_fact(*, component: str, resource_target: str, exposure: str, attributes: Mapping[str, object], provenance: tuple[TraceProvenance, ...]) -> ObservedArchitectureFact:
    payload = dict(attributes)
    payload["resource"] = resource_target
    source = _EXTERNAL_SUBJECT if exposure == "public" else component
    return _fact(source=source, target=component, attributes=payload, provenance=provenance)


def _k8s_component_labels(document: Mapping[str, object]) -> tuple[str | None, str | None]:
    metadata = _mapping(document.get("metadata"))
    direct = _string(_mapping(metadata.get("labels")).get("sdai.io/component"))
    template = _mapping(_mapping(document.get("spec")).get("template"))
    nested = _string(_mapping(_mapping(template.get("metadata")).get("labels")).get("sdai.io/component"))
    return direct, nested


def _k8s_container_ports(document: Mapping[str, object]) -> list[dict[str, object]]:
    spec = _mapping(document.get("spec"))
    template = _mapping(spec.get("template"))
    pod_spec = _mapping(template.get("spec")) if template else spec
    containers = pod_spec.get("containers")
    raw_ports: list[Mapping[str, object]] = []
    if isinstance(containers, list):
        for container in containers:
            ports = _mapping(container).get("ports")
            if isinstance(ports, list):
                for port in ports:
                    mapping = _mapping(port)
                    if mapping.get("containerPort") is not None:
                        raw_ports.append({"port": mapping.get("containerPort"), "protocol": mapping.get("protocol", "TCP")})
    return _ports(raw_ports)


def _k8s_service_ports(document: Mapping[str, object]) -> list[dict[str, object]]:
    values = _mapping(document.get("spec")).get("ports")
    raw_ports: list[Mapping[str, object]] = []
    if isinstance(values, list):
        for raw in values:
            item = _mapping(raw)
            if item.get("port") is not None:
                raw_ports.append({"port": item.get("port"), "targetPort": item.get("targetPort"), "protocol": item.get("protocol", "TCP")})
    return _ports(raw_ports)


def _parse_yaml_documents(text: str, *, source_path: str) -> tuple[Mapping[str, object], ...]:
    try:
        loaded = list(yaml.load_all(text, Loader=UniqueKeySafeLoader))
    except yaml.YAMLError as exc:
        raise _fail("SDAI-ARCH-DEPLOY-005", f"deployment YAML source is invalid: {source_path}") from exc
    if len(loaded) > DEPLOYMENT_MAX_DOCUMENTS:
        raise _fail("SDAI-ARCH-DEPLOY-005", f"deployment YAML source exceeds document limit: {source_path}")
    result: list[Mapping[str, object]] = []
    for index, raw in enumerate(loaded):
        if raw is None:
            continue
        try:
            normalized = normalize_structured_json(raw, max_nodes=200_000, max_depth=64)
        except ValueError as exc:
            raise _fail("SDAI-ARCH-DEPLOY-005", f"deployment YAML document {index + 1} is not bounded finite JSON") from exc
        if not isinstance(normalized, Mapping):
            raise _fail("SDAI-ARCH-DEPLOY-005", f"deployment YAML document {index + 1} must be a mapping")
        result.append(normalized)
    return tuple(result)


def _k8s_identity(document: Mapping[str, object], *, kind: str) -> tuple[str, str]:
    metadata = _mapping(document.get("metadata"))
    return (
        _safe_name(metadata.get("name"), label=f"Kubernetes {kind} metadata.name"),
        _safe_name(metadata.get("namespace") or "default", label=f"Kubernetes {kind} metadata.namespace"),
    )


def _kubernetes_facts(repository: ArchitectureRepositoryIndex, source: DeploymentSource, text: str) -> tuple[ObservedArchitectureFact, ...]:
    documents = _parse_yaml_documents(text, source_path=source.path)
    facts: list[ObservedArchitectureFact] = []
    service_components: dict[tuple[str, str], str] = {}

    for index, document in enumerate(documents, start=1):
        kind = _string(document.get("kind"))
        if kind not in _K8S_WORKLOAD_KINDS:
            continue
        name, namespace = _k8s_identity(document, kind=kind)
        direct, nested = _k8s_component_labels(document)
        component = _component_candidates(repository, source.path, name=name, explicit=(direct, nested))
        target = deployment_subject_id(source.source_id, "kubernetes", namespace, kind, name)
        facts.append(_fact(source=component, target=target, attributes={
            "role": "workload", "platform": "kubernetes", "sourceId": source.source_id,
            "environment": source.environment, "namespace": namespace, "workloadKind": kind,
            "name": name, "direction": "placement", "ports": _k8s_container_ports(document),
        }, provenance=_provenance(source.path, index, f"Kubernetes {kind} {namespace}/{name}")))

    for document in documents:
        if _string(document.get("kind")) != "Service":
            continue
        name, namespace = _k8s_identity(document, kind="Service")
        explicit = _string(_mapping(_mapping(document.get("metadata")).get("labels")).get("sdai.io/component"))
        component = _component_candidates(repository, source.path, name=name, explicit=(explicit,))
        key = (namespace, name)
        previous = service_components.get(key)
        if previous is not None and previous != component:
            raise _fail("SDAI-ARCH-DEPLOY-004", f"Kubernetes Service {namespace}/{name} has conflicting component mappings")
        service_components[key] = component

    for index, document in enumerate(documents, start=1):
        kind = _string(document.get("kind"))
        if kind not in {"Service", "Ingress"}:
            continue
        name, namespace = _k8s_identity(document, kind=kind)
        explicit = _string(_mapping(_mapping(document.get("metadata")).get("labels")).get("sdai.io/component"))
        if kind == "Service":
            component = service_components[(namespace, name)]
            spec = _mapping(document.get("spec"))
            service_type = _string(spec.get("type")) or "ClusterIP"
            exposure = "public" if service_type in {"LoadBalancer", "NodePort"} else "internal"
            ports = _k8s_service_ports(document)
        else:
            if explicit is not None or name in repository.component_ids or repository.owner_for_relative_path(source.path) is not None:
                component = _component_candidates(repository, source.path, name=name, explicit=(explicit,))
            else:
                spec = _mapping(document.get("spec"))
                backend_names: set[str] = set()
                default_name = _string(_mapping(_mapping(spec.get("defaultBackend")).get("service")).get("name"))
                if default_name is not None:
                    backend_names.add(default_name)
                rules = spec.get("rules")
                if isinstance(rules, list):
                    for rule in rules:
                        paths = _mapping(_mapping(rule).get("http")).get("paths")
                        if not isinstance(paths, list):
                            continue
                        for item in paths:
                            backend = _mapping(_mapping(item).get("backend"))
                            service_name = _string(_mapping(backend.get("service")).get("name"))
                            if service_name is not None:
                                backend_names.add(service_name)
                if not backend_names or any((namespace, backend) not in service_components for backend in backend_names):
                    raise _fail("SDAI-ARCH-DEPLOY-004", f"Kubernetes Ingress {name!r} has ambiguous/unresolved component backends")
                resolved = {service_components[(namespace, backend)] for backend in backend_names}
                if len(resolved) != 1:
                    raise _fail("SDAI-ARCH-DEPLOY-004", f"Kubernetes Ingress {name!r} has ambiguous/unresolved component backends")
                component = next(iter(resolved))
            spec = _mapping(document.get("spec"))
            exposure = "public"
            ports = [{"port": 443, "protocol": "HTTPS"}] if isinstance(spec.get("tls"), list) and spec.get("tls") else [{"port": 80, "protocol": "HTTP"}]
        resource_target = deployment_subject_id(source.source_id, "kubernetes", namespace, kind, name)
        facts.append(_exposure_fact(component=component, resource_target=resource_target, exposure=exposure, attributes={
            "role": "exposure", "platform": "kubernetes", "sourceId": source.source_id,
            "environment": source.environment, "namespace": namespace, "resourceKind": kind,
            "name": name, "exposure": exposure, "direction": "inbound", "ports": ports,
        }, provenance=_provenance(source.path, index, f"Kubernetes {kind} {namespace}/{name}")))
    return tuple(facts)


def _compose_labels(service: Mapping[str, object]) -> str | None:
    explicit = _string(service.get("x-sdai-component"))
    labels = service.get("labels")
    values: set[str] = set()
    if explicit is not None:
        values.add(explicit)
    if isinstance(labels, Mapping):
        value = _string(labels.get("sdai.component"))
        if value is not None:
            values.add(value)
    elif isinstance(labels, list):
        for item in labels:
            if isinstance(item, str) and item.startswith("sdai.component="):
                values.add(item.split("=", 1)[1])
    if len(values) > 1:
        raise _fail("SDAI-ARCH-DEPLOY-004", "Compose service has conflicting sdai component labels")
    return next(iter(values), None)


def _compose_ports(values: object) -> tuple[list[dict[str, object]], str | None]:
    if values is None:
        return [], None
    if not isinstance(values, list):
        raise _fail("SDAI-ARCH-DEPLOY-005", "Compose ports must be a list")
    raw_ports: list[Mapping[str, object]] = []
    exposure = "internal"
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool):
            raw_ports.append({"port": value, "protocol": "TCP"})
            continue
        if isinstance(value, Mapping):
            protocol_value = value.get("protocol", "tcp")
            if not isinstance(protocol_value, str):
                raise _fail("SDAI-ARCH-DEPLOY-005", "Compose port protocol must be text")
            item: dict[str, object] = {"port": value.get("target"), "protocol": protocol_value.upper()}
            published = value.get("published")
            if published is not None:
                item["published"] = published
                if _string(value.get("host_ip")) not in {"127.0.0.1", "::1", "localhost"}:
                    exposure = "public"
            raw_ports.append(item)
            continue
        if not isinstance(value, str):
            raise _fail("SDAI-ARCH-DEPLOY-005", "Compose port declaration is unsupported")
        text = value.strip()
        protocol = "TCP"
        if "/" in text:
            text, protocol = text.rsplit("/", 1)
            protocol = protocol.upper()
        parts = text.split(":")
        host_ip: str | None = None
        if len(parts) == 1:
            target_text, published_text = parts[0], None
        elif len(parts) == 2:
            published_text, target_text = parts
        elif len(parts) == 3:
            host_ip, published_text, target_text = parts
        else:
            raise _fail("SDAI-ARCH-DEPLOY-005", "Compose port declaration is unsupported")
        if not target_text.isdigit():
            raise _fail("SDAI-ARCH-DEPLOY-005", "Compose target port must be literal")
        item = {"port": int(target_text), "protocol": protocol}
        if published_text is not None:
            if not published_text.isdigit():
                raise _fail("SDAI-ARCH-DEPLOY-005", "Compose published port must be literal")
            item["published"] = int(published_text)
            if host_ip not in {"127.0.0.1", "::1", "localhost"}:
                exposure = "public"
        raw_ports.append(item)
    return _ports(raw_ports), exposure if raw_ports else None


def _compose_facts(repository: ArchitectureRepositoryIndex, source: DeploymentSource, text: str) -> tuple[ObservedArchitectureFact, ...]:
    documents = _parse_yaml_documents(text, source_path=source.path)
    if len(documents) != 1:
        raise _fail("SDAI-ARCH-DEPLOY-005", "Compose source must contain exactly one YAML document")
    services = documents[0].get("services")
    if not isinstance(services, Mapping):
        raise _fail("SDAI-ARCH-DEPLOY-005", "Compose source requires a services mapping")
    components: dict[str, str] = {}
    facts: list[ObservedArchitectureFact] = []
    namespace = f"compose:{_slug(source.source_id, limit=40)}"
    for raw_name in sorted(services):
        name = _safe_name(raw_name, label="Compose service name")
        service = _mapping(services[raw_name])
        component = _component_candidates(repository, source.path, name=name, explicit=(_compose_labels(service),))
        target = deployment_subject_id(source.source_id, "compose", namespace, "service", name)
        ports, exposure = _compose_ports(service.get("ports"))
        components[name] = component
        facts.append(_fact(source=component, target=target, attributes={
            "role": "workload", "platform": "compose", "sourceId": source.source_id,
            "environment": source.environment, "namespace": namespace, "workloadKind": "service",
            "name": name, "direction": "placement", "ports": ports,
        }, provenance=_provenance(source.path, 1, f"Compose service {name}")))
        if exposure is not None:
            facts.append(_exposure_fact(component=component, resource_target=target, exposure=exposure, attributes={
                "role": "exposure", "platform": "compose", "sourceId": source.source_id,
                "environment": source.environment, "namespace": namespace, "resourceKind": "service",
                "name": name, "exposure": exposure, "direction": "inbound", "ports": ports,
            }, provenance=_provenance(source.path, 1, f"Compose service exposure {name}")))
    for raw_name in sorted(services):
        name = _safe_name(raw_name, label="Compose service name")
        service = _mapping(services[raw_name])
        dependencies = service.get("depends_on")
        if dependencies is None:
            names: list[str] = []
        elif isinstance(dependencies, list):
            if any(not isinstance(item, str) for item in dependencies):
                raise _fail("SDAI-ARCH-DEPLOY-005", f"Compose service {name!r} depends_on must use literal service names")
            names = list(dependencies)
        elif isinstance(dependencies, Mapping):
            if any(not isinstance(item, str) for item in dependencies):
                raise _fail("SDAI-ARCH-DEPLOY-005", f"Compose service {name!r} depends_on keys must be text")
            names = list(dependencies)
        else:
            raise _fail("SDAI-ARCH-DEPLOY-005", f"Compose service {name!r} depends_on is unsupported")
        for dependency in sorted(set(names)):
            if dependency not in components:
                raise _fail("SDAI-ARCH-DEPLOY-005", f"Compose service {name!r} depends on unknown service {dependency!r}")
            source_component, target_component = components[name], components[dependency]
            if source_component != target_component:
                facts.append(_fact(source=source_component, target=target_component, attributes={
                    "role": "service-dependency", "platform": "compose", "sourceId": source.source_id,
                    "environment": source.environment, "dependency": dependency, "direction": "outbound",
                }, provenance=_provenance(source.path, 1, f"Compose depends_on {name} -> {dependency}")))
    return tuple(facts)


def _strip_hcl_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    state = "code"
    escaped = False
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "string":
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                state = "code"
            index += 1
            continue
        if state == "line":
            if char == "\n":
                output.append("\n")
                state = "code"
            else:
                output.append(" ")
            index += 1
            continue
        if state == "block":
            if char == "*" and nxt == "/":
                output.extend((" ", " "))
                index += 2
                state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if char == '"':
            output.append(char)
            state = "string"
            index += 1
        elif char == "#" or (char == "/" and nxt == "/"):
            output.append(" ")
            if char == "/":
                output.append(" ")
                index += 1
            index += 1
            state = "line"
        elif char == "/" and nxt == "*":
            output.extend((" ", " "))
            index += 2
            state = "block"
        else:
            output.append(char)
            index += 1
    return "".join(output)


def _matching_brace(text: str, start: int) -> int:
    depth = 0
    quote = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = False
            continue
        if char == '"':
            quote = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise _fail("SDAI-ARCH-DEPLOY-005", "Terraform resource block has unmatched braces")


def _terraform_resources(text: str) -> tuple[_TerraformResource, ...]:
    cleaned = _strip_hcl_comments(text)
    header = re.compile(r'\bresource\s+"(?P<type>[A-Za-z0-9_-]+)"\s+"(?P<name>[A-Za-z0-9_-]+)"\s*\{')
    resources: list[_TerraformResource] = []
    position = 0
    while True:
        match = header.search(cleaned, position)
        if match is None:
            break
        opening = cleaned.find("{", match.start())
        closing = _matching_brace(cleaned, opening)
        line = cleaned.count("\n", 0, match.start()) + 1
        resource_type, name = match.group("type"), match.group("name")
        resources.append(_TerraformResource(resource_type, name, cleaned[opening + 1 : closing], line, f"{resource_type}.{name}"))
        position = closing + 1
        if len(resources) > DEPLOYMENT_MAX_DOCUMENTS:
            raise _fail("SDAI-ARCH-DEPLOY-005", "Terraform source exceeds resource limit")
    return tuple(resources)


def _hcl_literal(block: str, keys: tuple[str, ...], *, required_if_present: bool = True) -> str | None:
    key_pattern = "|".join(re.escape(key) for key in keys)
    literal = re.search(rf'(?:^|\n)\s*(?:"?(?:{key_pattern})"?)\s*=\s*"(?P<value>[^"\n]{{1,256}})"', block, re.IGNORECASE)
    if literal is not None:
        return literal.group("value")
    if required_if_present and re.search(rf'(?:^|\n)\s*(?:"?(?:{key_pattern})"?)\s*=', block, re.IGNORECASE):
        raise _fail("SDAI-ARCH-DEPLOY-005", "Terraform SDAI deployment metadata must use literal strings")
    return None


def _hcl_port(block: str) -> int | None:
    match = re.search(r'(?:^|\n)\s*(?:"?(?:sdai_port|sdai:port)"?)\s*=\s*"?(?P<value>[0-9]{1,5})"?', block, re.IGNORECASE)
    if match is not None:
        value = int(match.group("value"))
        if not 1 <= value <= 65535:
            raise _fail("SDAI-ARCH-DEPLOY-005", "Terraform SDAI port is outside the valid range")
        return value
    if re.search(r'(?:^|\n)\s*(?:"?(?:sdai_port|sdai:port)"?)\s*=', block, re.IGNORECASE):
        raise _fail("SDAI-ARCH-DEPLOY-005", "Terraform SDAI port must be a literal integer")
    return None


def _terraform_facts(repository: ArchitectureRepositoryIndex, source: DeploymentSource, text: str) -> tuple[ObservedArchitectureFact, ...]:
    resources = _terraform_resources(text)
    facts: list[ObservedArchitectureFact] = []
    components_by_ref: dict[str, str] = {}
    for resource in resources:
        explicit = _hcl_literal(resource.block, ("sdai_component", "sdai:component"))
        if explicit is None and resource.name not in repository.component_ids:
            continue
        component = _component_candidates(repository, source.path, name=resource.name, explicit=(explicit,))
        namespace = _safe_name(_hcl_literal(resource.block, ("sdai_namespace", "sdai:namespace")) or "terraform", label="Terraform SDAI namespace")
        exposure = _hcl_literal(resource.block, ("sdai_exposure", "sdai:exposure"))
        if exposure is not None and exposure not in {"public", "internal"}:
            raise _fail("SDAI-ARCH-DEPLOY-005", "Terraform SDAI exposure must be public or internal")
        protocol = (_hcl_literal(resource.block, ("sdai_protocol", "sdai:protocol")) or "TCP").upper()
        if protocol not in _PORT_PROTOCOLS:
            raise _fail("SDAI-ARCH-DEPLOY-005", "Terraform SDAI protocol is unsupported")
        port = _hcl_port(resource.block)
        target = deployment_subject_id(source.source_id, "terraform", namespace, resource.resource_type, resource.name)
        components_by_ref[resource.reference] = component
        ports = [] if port is None else [{"port": port, "protocol": protocol}]
        facts.append(_fact(source=component, target=target, attributes={
            "role": "workload", "platform": "terraform", "sourceId": source.source_id,
            "environment": source.environment, "namespace": namespace, "workloadKind": resource.resource_type,
            "name": resource.name, "direction": "placement", "ports": ports,
        }, provenance=_provenance(source.path, resource.line, f"Terraform resource {resource.reference}")))
        if exposure is not None:
            facts.append(_exposure_fact(component=component, resource_target=target, exposure=exposure, attributes={
                "role": "exposure", "platform": "terraform", "sourceId": source.source_id,
                "environment": source.environment, "namespace": namespace, "resourceKind": resource.resource_type,
                "name": resource.name, "exposure": exposure, "direction": "inbound", "ports": ports,
            }, provenance=_provenance(source.path, resource.line, f"Terraform exposure {resource.reference}")))
    refs = set(components_by_ref)
    for resource in resources:
        source_component = components_by_ref.get(resource.reference)
        if source_component is None:
            continue
        depends = re.search(r"\bdepends_on\s*=\s*\[(?P<items>[^\]]*)\]", resource.block, re.DOTALL)
        if depends is None:
            continue
        for dependency in sorted(set(re.findall(r"\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", depends.group("items")))):
            if dependency not in refs:
                continue
            target_component = components_by_ref[dependency]
            if target_component != source_component:
                facts.append(_fact(source=source_component, target=target_component, attributes={
                    "role": "service-dependency", "platform": "terraform", "sourceId": source.source_id,
                    "environment": source.environment, "dependency": dependency, "direction": "outbound",
                }, provenance=_provenance(source.path, resource.line, f"Terraform depends_on {resource.reference} -> {dependency}")))
    return tuple(facts)


def _aggregate(values: Iterable[ObservedArchitectureFact]) -> tuple[ObservedArchitectureFact, ...]:
    groups: dict[str, list[ObservedArchitectureFact]] = {}
    workload_targets: dict[str, set[tuple[str, str]]] = {}
    for item in values:
        attrs = json.loads(json.dumps(dict(item.attributes), sort_keys=True, separators=(",", ":")))
        if attrs.get("role") == "workload":
            workload_targets.setdefault(item.target, set()).add((item.source, item.semantic_key))
        groups.setdefault(item.semantic_key, []).append(item)
    for target, identities in sorted(workload_targets.items()):
        if len(identities) > 1:
            raise _fail("SDAI-ARCH-DEPLOY-006", f"deployment workload identity {target!r} has conflicting observations")
    result: list[ObservedArchitectureFact] = []
    for key in sorted(groups):
        items = groups[key]
        first = items[0]
        provenance: dict[tuple[str, int], TraceProvenance] = {}
        for item in items:
            for value in item.provenance:
                previous = provenance.get(value.location)
                if previous is None or (value.detail or "") < (previous.detail or ""):
                    provenance[value.location] = value
        result.append(ObservedArchitectureFact(
            kind=first.kind, source=first.source, target=first.target, attributes=first.attributes,
            provenance=tuple(sorted(provenance.values(), key=lambda item: (item.source.casefold(), item.source, item.line, item.detail or ""))),
        ))
    if len(result) > DEPLOYMENT_MAX_FACTS:
        raise _fail("SDAI-ARCH-DEPLOY-006", "deployment observation exceeds fact limit")
    return tuple(result)


def _placements(facts: Iterable[ObservedArchitectureFact]) -> dict[str, list[_Placement]]:
    result: dict[str, list[_Placement]] = {}
    for fact in facts:
        attrs = json.loads(json.dumps(dict(fact.attributes), sort_keys=True, separators=(",", ":")))
        if attrs.get("role") != "workload":
            continue
        environment, namespace = attrs.get("environment"), attrs.get("namespace")
        if isinstance(environment, str) and isinstance(namespace, str):
            result.setdefault(fact.source, []).append(_Placement(fact.source, fact.target, environment, namespace, fact.provenance))
    return result


def _constraint_facts(approved: ApprovedArchitecture, observed: tuple[ObservedArchitectureFact, ...]) -> tuple[ObservedArchitectureFact, ...]:
    placements = _placements(observed)
    component_ids = {component.component_id for component in approved.topology.components}
    result: list[ObservedArchitectureFact] = []
    for fact in approved.topology.facts:
        if fact.kind is not ArchitectureFactKind.DEPLOYMENT:
            continue
        attrs = json.loads(json.dumps(dict(fact.attributes), sort_keys=True, separators=(",", ":")))
        role = attrs.get("role")
        if role not in {"co-location", "isolation"}:
            continue
        if fact.source not in component_ids or fact.target not in component_ids:
            raise _fail("SDAI-ARCH-DEPLOY-007", f"deployment constraint {fact.fact_id!r} must reference declared components")
        if attrs.get("scope") != "namespace":
            raise _fail("SDAI-ARCH-DEPLOY-007", f"deployment constraint {fact.fact_id!r} supports only namespace scope")
        environment = attrs.get("environment")
        if environment is not None:
            environment = _safe_environment(environment)
        left = [item for item in placements.get(fact.source, []) if environment is None or item.environment == environment]
        right = [item for item in placements.get(fact.target, []) if environment is None or item.environment == environment]
        if not left or not right:
            continue
        shared = [(a, b) for a in left for b in right if a.environment == b.environment and a.namespace == b.namespace]
        condition = bool(shared) if role == "co-location" else not shared
        if not condition:
            continue
        proof = (
            tuple(p for pair in shared for placement in pair for p in placement.provenance)
            if role == "co-location"
            else tuple(p for placement in (*left, *right) for p in placement.provenance)
        )
        unique = {(p.source, p.line, p.detail or ""): p for p in proof}
        result.append(_fact(source=fact.source, target=fact.target, attributes=fact.attributes,
            provenance=tuple(sorted(unique.values(), key=lambda p: (p.source, p.line, p.detail or "")))))
    return tuple(result)


class DeploymentTopologyObserver:
    """Observe version-controlled deployment topology without executing deployment tooling."""

    observer_id = DEPLOYMENT_OBSERVER_ID

    def observe(self, project_root: Path, approved: ApprovedArchitecture) -> ArchitectureObservation:
        if not isinstance(approved, ApprovedArchitecture):
            raise _fail("SDAI-ARCH-DEPLOY-001", "deployment observation requires validated approved architecture")
        root = project_root.resolve()
        repository = ArchitectureRepositoryIndex(root, approved.topology.components)
        facts: list[ObservedArchitectureFact] = []
        for source in load_deployment_sources(root):
            data = _read_bytes(root, source.path, maximum=DEPLOYMENT_SOURCE_MAX_BYTES, label=f"deployment source {source.source_id}")
            text = _decode(data, label=f"deployment source {source.source_id}")
            if source.kind == "kubernetes":
                facts.extend(_kubernetes_facts(repository, source, text))
            elif source.kind == "compose":
                facts.extend(_compose_facts(repository, source, text))
            elif source.kind == "terraform":
                facts.extend(_terraform_facts(repository, source, text))
            else:
                raise _fail("SDAI-ARCH-DEPLOY-002", f"unsupported deployment source kind: {source.kind}")
        canonical = _aggregate(facts)
        relations = _constraint_facts(approved, canonical)
        return ArchitectureObservation(self.observer_id, _aggregate((*canonical, *relations)))


__all__ = [
    "DEPLOYMENT_OBSERVER_ID", "DEPLOYMENT_SOURCES_API_VERSION", "DEPLOYMENT_SOURCES_PATH",
    "DeploymentSource", "DeploymentTopologyObserver", "deployment_subject_id", "load_deployment_sources",
]
