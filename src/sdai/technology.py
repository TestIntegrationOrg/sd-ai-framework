from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tomllib
from typing import Callable
import xml.etree.ElementTree as ET

import yaml

from sdai.path_safety import ensure_within_project
from sdai.text import TextEncodingError, read_utf8_text


class TechnologyDetectionError(RuntimeError):
    """Raised when explicit SDAI technology configuration is invalid."""


TECHNOLOGY_CONFIG = ".sdai/technology.yaml"
CATEGORIES = (
    "languages",
    "frameworks",
    "build_tools",
    "platforms",
    "libraries",
    "testing",
)

_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".gradle",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "build",
    "dist",
    "coverage",
    "__pycache__",
}
_TECH_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._+-]{0,62}[a-z0-9])?$")
_PS_REQUIRES = re.compile(
    r"(?im)^\s*#requires\s+-version\s+([0-9]+(?:\.[0-9]+){0,3})\b"
)
_GRADLE_JAVA = re.compile(
    r"(?:JavaLanguageVersion\.of\(|sourceCompatibility\s*=\s*(?:JavaVersion\.VERSION_)?)"
    r"([0-9_]+)"
)
_GRADLE_SPRING = re.compile(
    r"id\s*\(?['\"]org\.springframework\.boot['\"]\)?"
    r"\s*version\s*['\"]([^'\"]+)['\"]"
)
_GRADLE_KOTLIN = re.compile(
    r"kotlin\(['\"]jvm['\"]\)\s*version\s*['\"]([^'\"]+)['\"]"
)
_DEPENDENCY = re.compile(
    r"^\s*([A-Za-z0-9_.@/+:-]+)\s*(?:[<>=~!^ ]+\s*)?([0-9][^ ;,]*)?\s*$"
)


@dataclass(frozen=True)
class TechnologyEvidence:
    source: str
    detector: str
    version: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": self.source,
            "detector": self.detector,
        }
        if self.version is not None:
            payload["version"] = self.version
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class TechnologyFact:
    category: str
    name: str
    version: str | None
    version_source: str
    detected_versions: tuple[str, ...]
    declared: bool
    evidence: tuple[TechnologyEvidence, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "name": self.name,
            "version": self.version,
            "version_source": self.version_source,
            "detected_versions": list(self.detected_versions),
            "declared": self.declared,
            "evidence": [item.as_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class TechnologyFinding:
    code: str
    severity: str
    message: str
    source: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.source is not None:
            payload["source"] = self.source
        return payload


@dataclass(frozen=True)
class TechnologyReport:
    technologies: tuple[TechnologyFact, ...]
    findings: tuple[TechnologyFinding, ...]
    config_source: str | None

    def by_category(self) -> dict[str, tuple[TechnologyFact, ...]]:
        return {
            category: tuple(
                item for item in self.technologies if item.category == category
            )
            for category in CATEGORIES
        }

    def as_dict(self) -> dict[str, object]:
        grouped = self.by_category()
        return {
            "version": 1,
            "config_source": self.config_source,
            "technologies": {
                category: [item.as_dict() for item in grouped[category]]
                for category in CATEGORIES
            },
            "findings": [item.as_dict() for item in self.findings],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class _Detected:
    category: str
    name: str
    evidence: TechnologyEvidence


@dataclass(frozen=True)
class _Declared:
    category: str
    name: str
    version: str | None
    evidence: TechnologyEvidence


def _fail(code: str, message: str) -> TechnologyDetectionError:
    return TechnologyDetectionError(f"{code}: {message}")


def _portable(root: Path, path: Path) -> str:
    safe = ensure_within_project(root, path, label="technology evidence")
    return safe.relative_to(root.resolve()).as_posix()


def _read(
    root: Path,
    path: Path,
    findings: list[TechnologyFinding],
) -> str | None:
    source = _portable(root, path)
    try:
        return read_utf8_text(path)
    except (TextEncodingError, OSError) as exc:
        findings.append(
            TechnologyFinding(
                "SDAI-TECH-002",
                "warning",
                f"unable to read technology evidence: {exc}",
                source,
            )
        )
        return None


def _add(
    detected: list[_Detected],
    category: str,
    name: str,
    source: str,
    detector: str,
    *,
    version: str | None = None,
    detail: str | None = None,
) -> None:
    detected.append(
        _Detected(
            category,
            name,
            TechnologyEvidence(source, detector, version, detail),
        )
    )


def _repository_files(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(
            [
                name
                for name in dirs
                if name not in _EXCLUDED_DIRS
                and not (current_path / name).is_symlink()
                and not (
                    current_path == root / "specs"
                    and name == "archive"
                )
            ],
            key=str.casefold,
        )
        for filename in sorted(files, key=str.casefold):
            path = current_path / filename
            if not path.is_symlink():
                paths.append(path)
    return tuple(paths)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(parent: ET.Element, name: str) -> str | None:
    for child in parent:
        if _local(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def _resolve_property(value: str | None, properties: dict[str, str]) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    match = re.fullmatch(r"\$\{([^}]+)\}", normalized)
    return properties.get(match.group(1)) if match else normalized


def _detect_pom(
    root: Path,
    path: Path,
    detected: list[_Detected],
    findings: list[TechnologyFinding],
) -> None:
    source = _portable(root, path)
    text = _read(root, path, findings)
    if text is None:
        return
    try:
        project = ET.fromstring(text)
    except ET.ParseError as exc:
        findings.append(
            TechnologyFinding(
                "SDAI-TECH-003",
                "warning",
                f"invalid Maven XML: {exc}",
                source,
            )
        )
        return

    _add(detected, "build_tools", "maven", source, "pom.xml")
    _add(detected, "languages", "java", source, "pom.xml")

    properties: dict[str, str] = {}
    for child in project:
        if _local(child.tag) == "properties":
            for prop in child:
                if prop.text and prop.text.strip():
                    properties[_local(prop.tag)] = prop.text.strip()
    java_version = next(
        (
            _resolve_property(properties[key], properties)
            for key in (
                "maven.compiler.release",
                "java.version",
                "maven.compiler.source",
                "maven.compiler.target",
            )
            if properties.get(key)
        ),
        None,
    )
    if java_version:
        _add(
            detected,
            "languages",
            "java",
            source,
            "maven-java-version",
            version=java_version,
        )

    parent = next(
        (item for item in project if _local(item.tag) == "parent"),
        None,
    )
    if parent is not None:
        group = _child_text(parent, "groupId")
        artifact = _child_text(parent, "artifactId")
        version = _resolve_property(_child_text(parent, "version"), properties)
        if (
            group == "org.springframework.boot"
            and artifact == "spring-boot-starter-parent"
        ):
            _add(
                detected,
                "frameworks",
                "spring-boot",
                source,
                "maven-parent",
                version=version,
            )

    for dependency in project.iter():
        if _local(dependency.tag) != "dependency":
            continue
        group = _resolve_property(_child_text(dependency, "groupId"), properties) or ""
        artifact = (
            _resolve_property(_child_text(dependency, "artifactId"), properties) or ""
        )
        version = _resolve_property(_child_text(dependency, "version"), properties)
        coordinate = f"{group}:{artifact}"
        if group == "org.springframework.boot" or artifact.startswith("spring-boot-"):
            _add(
                detected,
                "frameworks",
                "spring-boot",
                source,
                "maven-dependency",
                version=version,
                detail=coordinate,
            )
        if group == "io.quarkus" or artifact.startswith("quarkus-"):
            _add(
                detected,
                "frameworks",
                "quarkus",
                source,
                "maven-dependency",
                version=version,
                detail=coordinate,
            )
        if group == "software.amazon.awssdk":
            _add(
                detected,
                "platforms",
                "aws",
                source,
                "maven-dependency",
                detail=coordinate,
            )
            _add(
                detected,
                "libraries",
                "aws-sdk-java-v2",
                source,
                "maven-dependency",
                version=version,
                detail=coordinate,
            )
        if group == "net.jsign" or artifact == "jsign":
            _add(
                detected,
                "libraries",
                "jsign",
                source,
                "maven-dependency",
                version=version,
                detail=coordinate,
            )
        if group.startswith("org.mongodb"):
            _add(
                detected,
                "libraries",
                "mongodb",
                source,
                "maven-dependency",
                version=version,
                detail=coordinate,
            )
        if group.startswith("org.junit") or artifact.startswith("junit"):
            _add(
                detected,
                "testing",
                "junit",
                source,
                "maven-dependency",
                version=version,
                detail=coordinate,
            )


def _detect_gradle(
    root: Path,
    path: Path,
    detected: list[_Detected],
    findings: list[TechnologyFinding],
) -> None:
    source = _portable(root, path)
    text = _read(root, path, findings)
    if text is None:
        return
    _add(detected, "build_tools", "gradle", source, path.name)
    if re.search(
        r"(?m)(?:id\s*\(?['\"]java(?:-library)?['\"]|\bjava\s*\{)",
        text,
    ):
        _add(detected, "languages", "java", source, "gradle-plugin")
    kotlin = _GRADLE_KOTLIN.search(text)
    if kotlin:
        _add(
            detected,
            "languages",
            "kotlin",
            source,
            "gradle-plugin",
            version=kotlin.group(1),
        )
    elif "org.jetbrains.kotlin.jvm" in text or 'kotlin("jvm")' in text:
        _add(detected, "languages", "kotlin", source, "gradle-plugin")
    java = _GRADLE_JAVA.search(text)
    if java:
        version = java.group(1).replace("_", ".")
        if version.startswith("1."):
            version = version[2:]
        _add(
            detected,
            "languages",
            "java",
            source,
            "gradle-java-version",
            version=version,
        )
    spring = _GRADLE_SPRING.search(text)
    if spring:
        _add(
            detected,
            "frameworks",
            "spring-boot",
            source,
            "gradle-plugin",
            version=spring.group(1),
        )
    elif "org.springframework.boot" in text:
        _add(detected, "frameworks", "spring-boot", source, "gradle-signal")
    if "io.quarkus" in text:
        _add(detected, "frameworks", "quarkus", source, "gradle-signal")
    if "software.amazon.awssdk" in text:
        _add(detected, "platforms", "aws", source, "gradle-dependency")
        _add(
            detected,
            "libraries",
            "aws-sdk-java-v2",
            source,
            "gradle-dependency",
        )
    if "net.jsign" in text:
        _add(detected, "libraries", "jsign", source, "gradle-dependency")
    if "junit" in text.casefold():
        _add(detected, "testing", "junit", source, "gradle-dependency")


def _target_framework_version(tfm: str) -> str | None:
    match = re.match(
        r"net(?:coreapp|standard)?([0-9]+)(?:\.([0-9]+))?",
        tfm.casefold(),
    )
    if not match:
        return None
    major = match.group(1)
    minor = match.group(2)
    if minor is None and len(major) >= 2:
        return f"{major[:-1]}.{major[-1]}"
    return f"{major}.{minor or '0'}"


def _detect_csproj(
    root: Path,
    path: Path,
    detected: list[_Detected],
    findings: list[TechnologyFinding],
) -> None:
    source = _portable(root, path)
    text = _read(root, path, findings)
    if text is None:
        return
    try:
        project = ET.fromstring(text)
    except ET.ParseError as exc:
        findings.append(
            TechnologyFinding(
                "SDAI-TECH-003",
                "warning",
                f"invalid .csproj XML: {exc}",
                source,
            )
        )
        return

    target_frameworks: list[str] = []
    language_versions: list[str] = []
    for element in project.iter():
        local = _local(element.tag)
        if local in {"TargetFramework", "TargetFrameworks"} and element.text:
            target_frameworks.extend(
                value.strip()
                for value in element.text.split(";")
                if value.strip()
            )
        elif local == "LangVersion" and element.text and element.text.strip():
            language_versions.append(element.text.strip())

    _add(detected, "languages", "csharp", source, "csproj")
    for version in sorted(set(language_versions)):
        _add(
            detected,
            "languages",
            "csharp",
            source,
            "csproj-langversion",
            version=version,
        )

    runtime_versions = sorted(
        {
            version
            for tfm in target_frameworks
            if (version := _target_framework_version(tfm)) is not None
        }
    )
    if runtime_versions:
        for version in runtime_versions:
            _add(
                detected,
                "frameworks",
                "dotnet",
                source,
                "csproj-target-framework",
                version=version,
                detail=";".join(target_frameworks),
            )
    else:
        _add(
            detected,
            "frameworks",
            "dotnet",
            source,
            "csproj-target-framework",
            detail=";".join(target_frameworks) or None,
        )

    sdk = project.attrib.get("Sdk", "")
    if "Microsoft.NET.Sdk.Web" in sdk:
        if runtime_versions:
            for version in runtime_versions:
                _add(
                    detected,
                    "frameworks",
                    "aspnet-core",
                    source,
                    "csproj-sdk",
                    version=version,
                )
        else:
            _add(detected, "frameworks", "aspnet-core", source, "csproj-sdk")

    for element in project.iter():
        if _local(element.tag) != "PackageReference":
            continue
        package = element.attrib.get("Include") or element.attrib.get("Update") or ""
        version = element.attrib.get("Version") or _child_text(element, "Version")
        folded = package.casefold()
        if folded.startswith("awssdk."):
            _add(
                detected,
                "platforms",
                "aws",
                source,
                "nuget",
                detail=package,
            )
        if folded.startswith("microsoft.azure") or folded.startswith("azure."):
            _add(
                detected,
                "platforms",
                "azure",
                source,
                "nuget",
                detail=package,
            )
        if "mongodb" in folded:
            _add(
                detected,
                "libraries",
                "mongodb",
                source,
                "nuget",
                version=version,
                detail=package,
            )
        if folded in {"xunit", "nunit", "mstest.testframework"}:
            _add(
                detected,
                "testing",
                folded.split(".")[0],
                source,
                "nuget",
                version=version,
                detail=package,
            )


def _dependency(value: str) -> tuple[str, str | None]:
    match = _DEPENDENCY.match(value)
    if not match:
        return value.strip().casefold(), None
    return match.group(1).casefold(), match.group(2)


def _detect_pyproject(
    root: Path,
    path: Path,
    detected: list[_Detected],
    findings: list[TechnologyFinding],
) -> None:
    source = _portable(root, path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        findings.append(
            TechnologyFinding(
                "SDAI-TECH-003",
                "warning",
                f"invalid pyproject.toml: {exc}",
                source,
            )
        )
        return
    project = data.get("project") or {}
    if not isinstance(project, dict):
        project = {}
    python_constraint = project.get("requires-python")
    _add(
        detected,
        "languages",
        "python",
        source,
        "pyproject.toml",
        version=str(python_constraint) if python_constraint else None,
    )
    dependencies = list(project.get("dependencies") or [])
    optional = project.get("optional-dependencies") or {}
    if isinstance(optional, dict):
        for values in optional.values():
            if isinstance(values, list):
                dependencies.extend(values)
    for raw in dependencies:
        if not isinstance(raw, str):
            continue
        name, version = _dependency(raw)
        if name == "fastapi":
            _add(
                detected,
                "frameworks",
                "fastapi",
                source,
                "python-dependency",
                version=version,
            )
        elif name == "django":
            _add(
                detected,
                "frameworks",
                "django",
                source,
                "python-dependency",
                version=version,
            )
        elif name in {"boto3", "botocore"}:
            _add(detected, "platforms", "aws", source, "python-dependency")
            _add(
                detected,
                "libraries",
                "boto3",
                source,
                "python-dependency",
                version=version,
            )
        elif name == "pytest":
            _add(
                detected,
                "testing",
                "pytest",
                source,
                "python-dependency",
                version=version,
            )


def _detect_package_json(
    root: Path,
    path: Path,
    detected: list[_Detected],
    findings: list[TechnologyFinding],
) -> None:
    source = _portable(root, path)
    text = _read(root, path, findings)
    if text is None:
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        findings.append(
            TechnologyFinding(
                "SDAI-TECH-003",
                "warning",
                f"invalid package.json: {exc}",
                source,
            )
        )
        return
    if not isinstance(data, dict):
        findings.append(
            TechnologyFinding(
                "SDAI-TECH-003",
                "warning",
                "package.json must be an object",
                source,
            )
        )
        return

    _add(detected, "languages", "javascript", source, "package.json")
    engines = data.get("engines") or {}
    if isinstance(engines, dict) and engines.get("node"):
        _add(
            detected,
            "frameworks",
            "nodejs",
            source,
            "package-engines",
            version=str(engines["node"]),
        )
    manager = data.get("packageManager")
    if isinstance(manager, str) and "@" in manager:
        name, version = manager.split("@", 1)
        if name in {"npm", "pnpm", "yarn"}:
            _add(
                detected,
                "build_tools",
                name,
                source,
                "package-manager",
                version=version,
            )

    dependencies: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        values = data.get(key) or {}
        if isinstance(values, dict):
            dependencies.update(
                {str(name): str(version) for name, version in values.items()}
            )
    if "typescript" in dependencies:
        _add(
            detected,
            "languages",
            "typescript",
            source,
            "package-dependency",
            version=dependencies["typescript"],
        )
    frameworks = {
        "react": "react",
        "@angular/core": "angular",
        "express": "express",
        "next": "nextjs",
        "@nestjs/core": "nestjs",
    }
    for package, technology in frameworks.items():
        if package in dependencies:
            _add(
                detected,
                "frameworks",
                technology,
                source,
                "package-dependency",
                version=dependencies[package],
            )
    for package in dependencies:
        folded = package.casefold()
        if folded.startswith("@aws-sdk/"):
            _add(
                detected,
                "platforms",
                "aws",
                source,
                "package-dependency",
                detail=package,
            )
        elif folded.startswith("@azure/"):
            _add(
                detected,
                "platforms",
                "azure",
                source,
                "package-dependency",
                detail=package,
            )
        elif folded.startswith("@google-cloud/"):
            _add(
                detected,
                "platforms",
                "gcp",
                source,
                "package-dependency",
                detail=package,
            )
    for package in ("vitest", "jest", "mocha"):
        if package in dependencies:
            _add(
                detected,
                "testing",
                package,
                source,
                "package-dependency",
                version=dependencies[package],
            )


def _detect_go_mod(
    root: Path,
    path: Path,
    detected: list[_Detected],
    findings: list[TechnologyFinding],
) -> None:
    source = _portable(root, path)
    text = _read(root, path, findings)
    if text is None:
        return
    match = re.search(
        r"(?m)^go\s+([0-9]+(?:\.[0-9]+){1,2})\s*$",
        text,
    )
    _add(
        detected,
        "languages",
        "go",
        source,
        "go.mod",
        version=match.group(1) if match else None,
    )
    _add(detected, "build_tools", "go", source, "go.mod")
    if "github.com/aws/aws-sdk-go" in text:
        _add(detected, "platforms", "aws", source, "go-module")


def _detect_cargo(
    root: Path,
    path: Path,
    detected: list[_Detected],
    findings: list[TechnologyFinding],
) -> None:
    source = _portable(root, path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        findings.append(
            TechnologyFinding(
                "SDAI-TECH-003",
                "warning",
                f"invalid Cargo.toml: {exc}",
                source,
            )
        )
        return
    package = data.get("package") or {}
    rust_version = package.get("rust-version") if isinstance(package, dict) else None
    _add(
        detected,
        "languages",
        "rust",
        source,
        "Cargo.toml",
        version=str(rust_version) if rust_version else None,
    )
    _add(detected, "build_tools", "cargo", source, "Cargo.toml")


def _detect_powershell(
    root: Path,
    path: Path,
    detected: list[_Detected],
    findings: list[TechnologyFinding],
) -> None:
    source = _portable(root, path)
    text = _read(root, path, findings)
    if text is None:
        return
    match = _PS_REQUIRES.search(text)
    _add(
        detected,
        "languages",
        "powershell",
        source,
        "ps1",
        version=match.group(1) if match else None,
    )
    if re.search(r"(?i)\bDescribe\s+['\"]|\bIt\s+['\"]", text):
        _add(detected, "testing", "pester", source, "ps1-signal")


def _detect_simple(path: Path, source: str, detected: list[_Detected]) -> None:
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if name == "tsconfig.json":
        _add(detected, "languages", "typescript", source, "tsconfig.json")
    elif name == "dockerfile" or name.startswith("dockerfile."):
        _add(detected, "platforms", "docker", source, "dockerfile")
    elif suffix == ".tf":
        _add(detected, "build_tools", "terraform", source, "terraform-file")
    elif name == "go.work":
        _add(detected, "build_tools", "go", source, "go.work")


def _load_declared(root: Path) -> tuple[list[_Declared], str | None]:
    path = ensure_within_project(
        root,
        root / TECHNOLOGY_CONFIG,
        label="technology configuration",
    )
    if not path.is_file():
        return [], None
    try:
        raw = yaml.safe_load(read_utf8_text(path)) or {}
    except (yaml.YAMLError, TextEncodingError, OSError) as exc:
        raise _fail(
            "SDAI-TECH-001",
            f"invalid {TECHNOLOGY_CONFIG}: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise _fail(
            "SDAI-TECH-001",
            f"{TECHNOLOGY_CONFIG} must be a YAML mapping",
        )
    unknown = sorted(set(raw) - ({"version"} | set(CATEGORIES)))
    if unknown:
        raise _fail(
            "SDAI-TECH-001",
            "unknown technology configuration field(s): "
            + ", ".join(map(str, unknown)),
        )
    if raw.get("version") != 1:
        raise _fail(
            "SDAI-TECH-001",
            f"{TECHNOLOGY_CONFIG} version must be 1",
        )
    source = _portable(root, path)
    declared: list[_Declared] = []
    for category in CATEGORIES:
        values = raw.get(category) or {}
        if not isinstance(values, dict):
            raise _fail(
                "SDAI-TECH-001",
                f"{category} must map technology id to version/null",
            )
        for name, version in values.items():
            if not isinstance(name, str) or not _TECH_ID.fullmatch(name):
                raise _fail(
                    "SDAI-TECH-001",
                    f"invalid technology id '{name}' in {category}",
                )
            if version is not None and not isinstance(version, (str, int, float)):
                raise _fail(
                    "SDAI-TECH-001",
                    f"{category}.{name} version must be string/number/null",
                )
            normalized = None if version is None else str(version)
            declared.append(
                _Declared(
                    category,
                    name,
                    normalized,
                    TechnologyEvidence(
                        source,
                        "declared-pin" if normalized is not None else "declared",
                        normalized,
                    ),
                )
            )
    return declared, source


def _collapse(
    detected: list[_Detected],
    declared: list[_Declared],
    findings: list[TechnologyFinding],
) -> tuple[TechnologyFact, ...]:
    grouped: dict[tuple[str, str], list[TechnologyEvidence]] = {}
    declarations: dict[tuple[str, str], _Declared] = {}
    for item in detected:
        grouped.setdefault((item.category, item.name), []).append(item.evidence)
    for item in declared:
        key = (item.category, item.name)
        declarations[key] = item
        grouped.setdefault(key, []).append(item.evidence)

    facts: list[TechnologyFact] = []
    for category, name in sorted(grouped):
        evidence = tuple(
            sorted(
                grouped[(category, name)],
                key=lambda item: (
                    item.source,
                    item.detector,
                    item.version or "",
                    item.detail or "",
                ),
            )
        )
        detected_versions = tuple(
            sorted(
                {
                    item.version
                    for item in evidence
                    if not item.detector.startswith("declared")
                    and item.version is not None
                }
            )
        )
        declaration = declarations.get((category, name))

        if declaration is not None and declaration.version is not None:
            version = declaration.version
            version_source = "declared"
            if detected_versions and version not in detected_versions:
                findings.append(
                    TechnologyFinding(
                        "SDAI-TECH-004",
                        "warning",
                        f"declared {category}.{name} version '{version}' overrides "
                        f"detected version(s): {', '.join(detected_versions)}",
                        declaration.evidence.source,
                    )
                )
        elif len(detected_versions) == 1:
            version = detected_versions[0]
            version_source = "detected"
        elif len(detected_versions) > 1:
            version = None
            version_source = "ambiguous"
            findings.append(
                TechnologyFinding(
                    "SDAI-TECH-005",
                    "warning",
                    f"multiple detected versions for {category}.{name}: "
                    + ", ".join(detected_versions),
                )
            )
        else:
            version = None
            version_source = "none"

        facts.append(
            TechnologyFact(
                category=category,
                name=name,
                version=version,
                version_source=version_source,
                detected_versions=detected_versions,
                declared=declaration is not None,
                evidence=evidence,
            )
        )
    return tuple(facts)


def detect_technologies(project_root: Path) -> TechnologyReport:
    root = project_root.resolve()
    detected: list[_Detected] = []
    findings: list[TechnologyFinding] = []
    dispatch: dict[
        str,
        Callable[
            [Path, Path, list[_Detected], list[TechnologyFinding]],
            None,
        ],
    ] = {
        "pom.xml": _detect_pom,
        "build.gradle": _detect_gradle,
        "build.gradle.kts": _detect_gradle,
        "pyproject.toml": _detect_pyproject,
        "package.json": _detect_package_json,
        "go.mod": _detect_go_mod,
        "cargo.toml": _detect_cargo,
    }

    for path in _repository_files(root):
        source = _portable(root, path)
        name = path.name.casefold()
        detector = dispatch.get(name)
        if detector is not None:
            detector(root, path, detected, findings)
        elif path.suffix.casefold() == ".csproj":
            _detect_csproj(root, path, detected, findings)
        elif path.suffix.casefold() == ".ps1":
            _detect_powershell(root, path, detected, findings)
        _detect_simple(path, source, detected)

    declared, config_source = _load_declared(root)
    technologies = _collapse(detected, declared, findings)
    return TechnologyReport(
        technologies=technologies,
        findings=tuple(
            sorted(
                findings,
                key=lambda item: (
                    item.code,
                    item.source or "",
                    item.message,
                ),
            )
        ),
        config_source=config_source,
    )
