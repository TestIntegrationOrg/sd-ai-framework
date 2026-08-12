from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from sdai.entrypoint import main as sdai_main
from sdai.technology import TechnologyDetectionError, detect_technologies


def _init(root: Path) -> None:
    path = root / ".sdai" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("version: 1\n", encoding="utf-8")


def _facts(root: Path) -> dict[tuple[str, str], object]:
    report = detect_technologies(root)
    return {(item.category, item.name): item for item in report.technologies}


def test_maven_java_spring_aws_jsign_junit_detection_is_version_aware(tmp_path: Path) -> None:
    pom = tmp_path / "service" / "pom.xml"
    pom.parent.mkdir(parents=True)
    pom.write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.4.10</version>
  </parent>
  <properties>
    <java.version>17</java.version>
  </properties>
  <dependencies>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
    <dependency><groupId>software.amazon.awssdk</groupId><artifactId>kms</artifactId><version>2.29.27</version></dependency>
    <dependency><groupId>net.jsign</groupId><artifactId>jsign</artifactId><version>7.4</version></dependency>
    <dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId><version>5.10.2</version></dependency>
  </dependencies>
</project>
""",
        encoding="utf-8",
    )

    facts = _facts(tmp_path)

    assert facts[("languages", "java")].version == "17"
    assert facts[("frameworks", "spring-boot")].version == "3.4.10"
    assert facts[("build_tools", "maven")].version is None
    assert facts[("platforms", "aws")].version is None
    assert facts[("libraries", "aws-sdk-java-v2")].version == "2.29.27"
    assert facts[("libraries", "jsign")].version == "7.4"
    assert facts[("testing", "junit")].version == "5.10.2"
    assert all("\\" not in evidence.source for fact in facts.values() for evidence in fact.evidence)


def test_gradle_java_kotlin_spring_detection_is_conservative(tmp_path: Path) -> None:
    gradle = tmp_path / "build.gradle.kts"
    gradle.write_text(
        """plugins {
    java
    kotlin("jvm") version "2.0.21"
    id("org.springframework.boot") version "3.4.10"
}
java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }
dependencies {
    implementation("software.amazon.awssdk:kms:2.29.27")
    testImplementation("org.junit.jupiter:junit-jupiter:5.10.2")
}
""",
        encoding="utf-8",
    )

    facts = _facts(tmp_path)

    assert facts[("languages", "java")].version == "21"
    assert ("languages", "kotlin") in facts
    assert facts[("frameworks", "spring-boot")].version == "3.4.10"
    assert ("build_tools", "gradle") in facts
    assert ("platforms", "aws") in facts
    assert ("testing", "junit") in facts


def test_dotnet_csproj_detects_target_framework_web_sdk_nuget_and_test_framework(
    tmp_path: Path,
) -> None:
    project = tmp_path / "Api" / "Api.csproj"
    project.parent.mkdir(parents=True)
    project.write_text(
        """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="AWSSDK.S3" Version="3.7.400" />
    <PackageReference Include="MongoDB.Driver" Version="2.30.0" />
    <PackageReference Include="xunit" Version="2.9.2" />
  </ItemGroup>
</Project>
""",
        encoding="utf-8",
    )

    facts = _facts(tmp_path)

    assert facts[("languages", "csharp")].version == "8.0"
    assert facts[("frameworks", "dotnet")].version == "8.0"
    assert facts[("frameworks", "aspnet-core")].version == "8.0"
    assert ("platforms", "aws") in facts
    assert facts[("libraries", "mongodb")].version == "2.30.0"
    assert facts[("testing", "xunit")].version == "2.9.2"


def test_python_fastapi_aws_pytest_detection_uses_requires_python_constraint(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """[project]
name = "sample"
requires-python = ">=3.11,<3.13"
dependencies = ["fastapi>=0.115", "boto3>=1.35"]

[project.optional-dependencies]
test = ["pytest>=8.0"]
""",
        encoding="utf-8",
    )

    facts = _facts(tmp_path)

    assert facts[("languages", "python")].version == ">=3.11,<3.13"
    assert facts[("frameworks", "fastapi")].version == "0.115"
    assert ("platforms", "aws") in facts
    assert facts[("libraries", "boto3")].version == "1.35"
    assert facts[("testing", "pytest")].version == "8.0"


def test_node_typescript_react_angular_cloud_and_testing_signals(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "engines": {"node": ">=20 <25"},
                "packageManager": "pnpm@10.0.0",
                "dependencies": {
                    "react": "^19.0.0",
                    "@angular/core": "^20.0.0",
                    "@aws-sdk/client-s3": "^3.700.0",
                    "@azure/storage-blob": "^12.25.0",
                    "@google-cloud/storage": "^7.14.0",
                },
                "devDependencies": {"typescript": "^5.8.0", "vitest": "^3.0.0"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")

    facts = _facts(tmp_path)

    assert ("languages", "javascript") in facts
    assert facts[("languages", "typescript")].version == "^5.8.0"
    assert facts[("frameworks", "nodejs")].version == ">=20 <25"
    assert facts[("frameworks", "react")].version == "^19.0.0"
    assert facts[("frameworks", "angular")].version == "^20.0.0"
    assert facts[("build_tools", "pnpm")].version == "10.0.0"
    assert {name for category, name in facts if category == "platforms"} >= {"aws", "azure", "gcp"}
    assert facts[("testing", "vitest")].version == "^3.0.0"


def test_go_rust_and_powershell_detection(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "module example.com/sample\n\ngo 1.23.2\n\nrequire github.com/aws/aws-sdk-go-v2 v1.32.0\n",
        encoding="utf-8",
    )
    rust = tmp_path / "rust" / "Cargo.toml"
    rust.parent.mkdir()
    rust.write_text(
        """[package]
name = "sample"
version = "0.1.0"
rust-version = "1.82"
edition = "2021"
""",
        encoding="utf-8",
    )
    ps = tmp_path / "scripts" / "sign.ps1"
    ps.parent.mkdir()
    ps.write_text(
        "#requires -Version 7.4\nDescribe 'signing' { It 'works' { $true | Should -BeTrue } }\n",
        encoding="utf-8",
    )

    facts = _facts(tmp_path)

    assert facts[("languages", "go")].version == "1.23.2"
    assert ("build_tools", "go") in facts
    assert ("platforms", "aws") in facts
    assert facts[("languages", "rust")].version == "1.82"
    assert ("build_tools", "cargo") in facts
    assert facts[("languages", "powershell")].version == "7.4"
    assert ("testing", "pester") in facts


def test_multiple_detected_versions_are_ambiguous_until_explicit_pin(tmp_path: Path) -> None:
    for name, java_version in (("a", "17"), ("b", "21")):
        pom = tmp_path / name / "pom.xml"
        pom.parent.mkdir()
        pom.write_text(
            f"""<project><properties><java.version>{java_version}</java.version></properties></project>\n""",
            encoding="utf-8",
        )

    first = _facts(tmp_path)[("languages", "java")]
    assert first.version is None
    assert first.version_source == "ambiguous"
    assert first.detected_versions == ("17", "21")

    config = tmp_path / ".sdai" / "technology.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        yaml.safe_dump({"version": 1, "languages": {"java": "17"}}, sort_keys=False),
        encoding="utf-8",
    )

    report = detect_technologies(tmp_path)
    pinned = {(item.category, item.name): item for item in report.technologies}[("languages", "java")]
    assert pinned.version == "17"
    assert pinned.version_source == "declared"
    assert pinned.declared is True
    assert pinned.detected_versions == ("17", "21")
    assert not any(item.code == "SDAI-TECH-004" for item in report.findings)


def test_declared_version_override_is_retained_with_conflict_warning(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="sample"\nrequires-python=">=3.11,<3.13"\n',
        encoding="utf-8",
    )
    config = tmp_path / ".sdai" / "technology.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "languages": {"python": ">=3.12,<3.13"},
                "frameworks": {"fastapi": "0.115.0"},
                "platforms": {"aws": None},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = detect_technologies(tmp_path)
    facts = {(item.category, item.name): item for item in report.technologies}

    assert facts[("languages", "python")].version == ">=3.12,<3.13"
    assert facts[("languages", "python")].version_source == "declared"
    assert facts[("frameworks", "fastapi")].declared is True
    assert facts[("frameworks", "fastapi")].version == "0.115.0"
    assert facts[("platforms", "aws")].declared is True
    warning = next(item for item in report.findings if item.code == "SDAI-TECH-004")
    assert "python" in warning.message
    assert warning.source == ".sdai/technology.yaml"


def test_invalid_explicit_technology_config_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / ".sdai" / "technology.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "version: 1\nlanguages:\n  'Java / 17': 17\nunknown: true\n",
        encoding="utf-8",
    )

    with pytest.raises(TechnologyDetectionError, match="SDAI-TECH-001"):
        detect_technologies(tmp_path)


def test_scanner_skips_dependencies_archived_specs_and_symlinks(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="root"\nrequires-python=">=3.11"\n',
        encoding="utf-8",
    )
    node_modules = tmp_path / "node_modules" / "dependency"
    node_modules.mkdir(parents=True)
    (node_modules / "package.json").write_text('{"dependencies":{"react":"99.0.0"}}', encoding="utf-8")
    archived = tmp_path / "specs" / "archive" / "changes" / "old"
    archived.mkdir(parents=True)
    (archived / "package.json").write_text('{"dependencies":{"react":"98.0.0"}}', encoding="utf-8")

    outside = tmp_path.parent / f"{tmp_path.name}-outside-tech"
    outside.mkdir(exist_ok=True)
    (outside / "package.json").write_text('{"dependencies":{"react":"97.0.0"}}', encoding="utf-8")
    link = tmp_path / "linked-outside"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass

    facts = _facts(tmp_path)

    assert ("languages", "python") in facts
    assert ("frameworks", "react") not in facts


def test_json_output_is_deterministic_and_paths_are_portable_in_unicode_workspace(tmp_path: Path) -> None:
    root = tmp_path / "Enterprise Workspace Ω"
    root.mkdir()
    _init(root)
    service = root / "service café"
    service.mkdir()
    (service / "go.mod").write_text("module example.com/x\n\ngo 1.23\n", encoding="utf-8")

    first = detect_technologies(root).to_json()
    second = detect_technologies(root).to_json()

    assert first == second
    payload = json.loads(first)
    go = next(item for item in payload["technologies"]["languages"] if item["name"] == "go")
    assert go["evidence"][0]["source"] == "service café/go.mod"
    assert "\\" not in first


def test_cli_tech_detect_json_uses_initialized_project_boundary(tmp_path: Path, capsys) -> None:
    _init(tmp_path)
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.23\n", encoding="utf-8")

    assert sdai_main(["tech", "detect", "--json", "--path", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["version"] == 1
    assert payload["technologies"]["languages"][0]["name"] == "go"
