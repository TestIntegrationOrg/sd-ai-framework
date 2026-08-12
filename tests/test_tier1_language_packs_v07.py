from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest
import yaml

from sdai.evals import MockEvalExecutor, run_behavioral_eval
from sdai.language_packs import (
    TIER1_LANGUAGE_PACK_IDS,
    LanguagePackError,
    load_language_pack,
    validate_tier1_language_packs,
)
from sdai.skill_resolution import load_skill_metadata, resolve_skills


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _init(root: Path) -> None:
    path = root / ".sdai" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "operating_mode": "individual",
                "policy": {"repository": ".sdai/policy.yaml"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _agent(root: Path, name: str, capability: str) -> None:
    path = root / ".sdai" / "agents" / f"{name}.agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        + yaml.safe_dump(
            {
                "name": name,
                "description": f"Provider-neutral {name} role.",
                "capabilities": [capability],
                "skills": [],
                "execution_mode": "advisory",
                "providers": {},
            },
            sort_keys=False,
        )
        + "---\n\nOperate within the assigned semantic responsibility.\n",
        encoding="utf-8",
    )


def _copy_pack_skills(source_root: Path, target_root: Path, pack_id: str) -> tuple[str, ...]:
    pack = load_language_pack(source_root, pack_id)
    for name in pack.skills:
        source = source_root / ".agents" / "skills" / name
        target = target_root / ".agents" / "skills" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    return pack.skills


def test_all_tier1_language_packs_are_valid_and_have_improving_evals() -> None:
    root = _repo_root()

    packs = validate_tier1_language_packs(root)

    assert tuple(pack.id for pack in packs) == TIER1_LANGUAGE_PACK_IDS
    assert all(pack.version == "0.1.0" for pack in packs)
    assert all("\\" not in pack.source for pack in packs)
    all_skills = {name for pack in packs for name in pack.skills}
    assert all_skills == {
        "java-engineering",
        "spring-boot",
        "csharp-engineering",
        "aspnet-core",
        "python-engineering",
        "fastapi",
        "django",
        "javascript-engineering",
        "typescript-engineering",
        "nodejs-engineering",
        "react-engineering",
        "angular-engineering",
        "go-engineering",
        "powershell-engineering",
    }

    for name in sorted(all_skills):
        report = run_behavioral_eval(
            root,
            "skill",
            name,
            executor=MockEvalExecutor(),
            require_improvement=True,
        )
        assert report.passed is True, name
        assert report.candidate_score > report.baseline_score, name


def test_java_pack_composes_with_existing_semantic_role_types(tmp_path: Path) -> None:
    source = _repo_root()
    _init(tmp_path)
    _copy_pack_skills(source, tmp_path, "sdai-java")
    (tmp_path / "pom.xml").write_text(
        """<project>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.4.10</version>
  </parent>
  <properties><java.version>17</java.version></properties>
</project>
""",
        encoding="utf-8",
    )
    roles = {
        "architect": "architecture",
        "developer": "coding",
        "code-reviewer": "review",
        "tester": "testing",
        "security-reviewer": "security",
    }
    for role, capability in roles.items():
        _agent(tmp_path, role, capability)
        report = resolve_skills(
            tmp_path,
            agent_name=role,
            capability=capability,
        )
        assert report.selected == ("java-engineering", "spring-boot"), role

    agent_names = {path.stem.removesuffix(".agent") for path in (tmp_path / ".sdai" / "agents").glob("*.agent.md")}
    assert agent_names == set(roles)
    assert not any(name.startswith(("java-", "codex-", "claude-")) for name in agent_names)


@pytest.mark.parametrize(
    ("pack_id", "expected", "fixture"),
    [
        (
            "sdai-dotnet",
            {"csharp-engineering", "aspnet-core"},
            "dotnet",
        ),
        (
            "sdai-python",
            {"python-engineering", "fastapi", "django"},
            "python",
        ),
        (
            "sdai-typescript-javascript",
            {
                "javascript-engineering",
                "typescript-engineering",
                "nodejs-engineering",
                "react-engineering",
                "angular-engineering",
            },
            "typescript-javascript",
        ),
        ("sdai-go", {"go-engineering"}, "go"),
        ("sdai-powershell", {"powershell-engineering"}, "powershell"),
    ],
)
def test_tier1_pack_skills_resolve_from_repository_technology(
    tmp_path: Path,
    pack_id: str,
    expected: set[str],
    fixture: str,
) -> None:
    source = _repo_root()
    _init(tmp_path)
    _agent(tmp_path, "developer", "coding")
    _copy_pack_skills(source, tmp_path, pack_id)

    if fixture == "dotnet":
        (tmp_path / "Service.csproj").write_text(
            """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <LangVersion>12.0</LangVersion>
  </PropertyGroup>
</Project>
""",
            encoding="utf-8",
        )
    elif fixture == "python":
        (tmp_path / "pyproject.toml").write_text(
            """[project]
name = "sample"
requires-python = ">=3.11"
dependencies = ["fastapi>=0.115", "django>=5.0"]
""",
            encoding="utf-8",
        )
    elif fixture == "typescript-javascript":
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "engines": {"node": ">=20"},
                    "dependencies": {
                        "react": "^19.0.0",
                        "@angular/core": "^20.0.0",
                    },
                    "devDependencies": {"typescript": "^5.8.0"},
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    elif fixture == "go":
        (tmp_path / "go.mod").write_text(
            "module example.com/sample\n\ngo 1.23\n",
            encoding="utf-8",
        )
    elif fixture == "powershell":
        (tmp_path / "script.ps1").write_text(
            "#requires -Version 7.4\nWrite-Output 'ok'\n",
            encoding="utf-8",
        )
    else:  # pragma: no cover - parameterization owns this set
        raise AssertionError(fixture)

    report = resolve_skills(
        tmp_path,
        agent_name="developer",
        capability="coding",
    )

    assert set(report.selected) == expected
    positions = {name: index for index, name in enumerate(report.selected)}
    for name in expected:
        metadata = load_skill_metadata(tmp_path, name)
        for dependency in metadata.requires:
            assert positions[dependency] < positions[name]


def test_language_pack_validator_rejects_framework_without_core_dependency(
    tmp_path: Path,
) -> None:
    source = _repo_root()
    _init(tmp_path)
    pack = load_language_pack(source, "sdai-java")
    for name in pack.skills:
        src = source / ".agents" / "skills" / name
        dst = tmp_path / ".agents" / "skills" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
    manifest_source = source / ".sdai" / "extensions" / "packs" / "sdai-java.yaml"
    manifest_target = tmp_path / ".sdai" / "extensions" / "packs" / "sdai-java.yaml"
    manifest_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest_source, manifest_target)

    spring_sidecar = tmp_path / ".agents" / "skills" / "spring-boot" / "sdai.yaml"
    payload = yaml.safe_load(spring_sidecar.read_text(encoding="utf-8"))
    payload["requires"] = []
    spring_sidecar.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(
        LanguagePackError,
        match="SDAI-LANGPACK-004.*spring-boot.*core pack skill",
    ):
        load_language_pack(tmp_path, "sdai-java")
