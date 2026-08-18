from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from sdai.architecture_dependency_observer import (
    DEPENDENCY_OBSERVER_ID,
    DependencyImportObserver,
)
from sdai.architecture_drift import (
    ArchitectureDriftError,
    ArchitectureFactKind,
    compare_architecture,
    load_approved_architecture,
    load_architecture_topology,
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


FEATURE = "ARCH-DEP-217"


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


def _topology(root: Path, *, mode: str = "required", nested_parent: bool = False) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    approval = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    app_id = "shell" if nested_parent else "app"
    app_root = "src" if nested_parent else "src/app"
    return _write(
        feature / "architecture" / "approved-topology.yaml",
        f"""apiVersion: sdai.architecture-topology/v1
kind: ApprovedArchitecture
metadata:
  id: dependency-topology
  feature: {FEATURE}
  approvalEvidence: {approval}
spec:
  components:
    - id: {app_id}
      roots: [{app_root}]
      modulePrefixes: [acme.app, Acme.App, '@acme/app', github.com/acme/app, AcmeApp]
    - id: data
      roots: [src/data]
      modulePrefixes: [acme.data, Acme.Data, '@acme/data', github.com/acme/data, AcmeData]
  facts:
    - id: APP-DATA
      kind: dependency
      mode: {mode}
      source: {app_id}
      target: data
      attributes: {{}}
""",
    )


def _approve(root: Path) -> None:
    topology = load_architecture_topology(root, FEATURE)
    evidence_relative = f"specs/changes/{FEATURE}/evidence/architecture-approval.json"
    record = TraceEvidence(
        evidence_id="ARCH-APPROVAL-217",
        kind=EvidenceKind.APPROVAL,
        status=EvidenceStatus.PASSED,
        subject=topology.subject,
        git_commit=_git(root, "rev-parse", "HEAD"),
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.ARTIFACT,
                topology.source,
                topology.file_sha256,
            ),
        ),
        provenance=(TraceProvenance(evidence_relative, 1, detail="dependency topology approval"),),
        producer=EvidenceProducer("architecture-approver", None, None),
        result={
            "architectureApproval": {
                "featureId": FEATURE,
                "topologyId": topology.topology_id,
                "topologySha256": topology.sha256,
            }
        },
        tool="sdai-architecture-approval",
    )
    _write(root / evidence_relative, record.to_json())


def _project(tmp_path: Path, *, mode: str = "required", nested_parent: bool = False) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "dependency-observer@example.invalid")
    _git(root, "config", "user.name", "Dependency Observer Tests")
    _topology(root, mode=mode, nested_parent=nested_parent)
    _write(root / "src" / "data" / "repo.py", "# data component\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "add dependency topology")
    _approve(root)
    return root


def _observe(root: Path):
    approved = load_approved_architecture(root, FEATURE)
    return approved, DependencyImportObserver().observe(root, approved)


def test_tier1_language_imports_collapse_to_one_component_dependency_with_all_provenance(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(
        root / "src" / "app" / "python_app.py",
        """# from acme.data.fake import ignored
from acme.data import repo
""",
    )
    _write(
        root / "src" / "app" / "App.java",
        """/* import acme.data.Ignored; */
import acme.data.Repository;
class App {}
""",
    )
    _write(
        root / "src" / "app" / "App.kt",
        """// import acme.data.Ignored
import acme.data.Repository as Repo
class App
""",
    )
    _write(
        root / "src" / "app" / "App.cs",
        """// using Acme.Data.Ignored;
using Data = Acme.Data;
class App {}
""",
    )
    _write(
        root / "src" / "app" / "App.fs",
        """// open Acme.Data.Ignored
open Acme.Data
module App
""",
    )
    _write(
        root / "src" / "app" / "main.ts",
        """// import x from '@acme/data/ignored';
import client from '@acme/data/client';
const fake = \"require('@acme/data/ignored')\";
""",
    )
    _write(
        root / "src" / "app" / "main.js",
        """/* const ignored = require('@acme/data/ignored'); */
const client = require('@acme/data/client');
""",
    )
    _write(
        root / "src" / "app" / "main.go",
        """package app
/* import \"github.com/acme/data/ignored\" */
import data \"github.com/acme/data/client\"
""",
    )
    _write(
        root / "src" / "app" / "main.ps1",
        """# Import-Module AcmeIgnored
Import-Module AcmeData
""",
    )

    approved, observation = _observe(root)

    assert observation.observer_id == DEPENDENCY_OBSERVER_ID
    assert observation.to_json() == DependencyImportObserver().observe(root, approved).to_json()
    assert len(observation.facts) == 1
    fact = observation.facts[0]
    assert fact.kind is ArchitectureFactKind.DEPENDENCY
    assert fact.source == "app"
    assert fact.target == "data"
    assert dict(fact.attributes) == {}
    assert len(fact.provenance) == 9
    assert {item.source for item in fact.provenance} == {
        "src/app/python_app.py",
        "src/app/App.java",
        "src/app/App.kt",
        "src/app/App.cs",
        "src/app/App.fs",
        "src/app/main.ts",
        "src/app/main.js",
        "src/app/main.go",
        "src/app/main.ps1",
    }

    report = compare_architecture(approved, (observation,))
    assert report.findings == ()


def test_forbidden_coupling_is_reported_with_concrete_import_provenance(tmp_path: Path) -> None:
    root = _project(tmp_path, mode="forbidden")
    _write(root / "src" / "app" / "service.py", "from acme.data import repo\n")

    approved, observation = _observe(root)
    report = compare_architecture(approved, (observation,))

    forbidden = [item for item in report.findings if item.code == "ARCH-DRIFT-FORBIDDEN-PRESENT"]
    assert len(forbidden) == 1
    assert forbidden[0].approved_fact_id == "APP-DATA"
    assert forbidden[0].approved_provenance[0].source.endswith("approved-topology.yaml")
    assert forbidden[0].observed_provenance[0].source == "src/app/service.py"
    assert forbidden[0].observed_provenance[0].line == 1


def test_relative_javascript_import_resolves_across_component_roots(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root / "src" / "data" / "client.ts", "export const client = 1;\n")
    _write(
        root / "src" / "app" / "ui" / "main.ts",
        "import { client } from '../../data/client';\n",
    )

    _, observation = _observe(root)

    assert [(item.source, item.target) for item in observation.facts] == [("app", "data")]
    assert observation.facts[0].provenance[0].source == "src/app/ui/main.ts"


def test_unresolved_local_import_fails_closed_instead_of_becoming_external(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root / "src" / "app" / "main.ts", "import x from './missing';\n")

    with pytest.raises(ArchitectureDriftError, match="SDAI-ARCH-DEPENDENCY-004.*cannot be resolved"):
        _observe(root)


def test_external_dependency_is_preserved_as_stable_explicit_identity(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root / "src" / "app" / "service.py", "import thirdparty\n")

    approved, first = _observe(root)
    second = DependencyImportObserver().observe(root, approved)
    external = [item for item in first.facts if item.target.startswith("external:")]

    assert len(external) == 1
    assert external[0].source == "app"
    assert external[0].target.startswith("external:thirdparty:")
    assert external[0].target == [item for item in second.facts if item.target.startswith("external:")][0].target
    assert external[0].provenance[0].detail == "python import thirdparty"


def test_nested_component_roots_use_longest_match_without_duplicate_file_observation(tmp_path: Path) -> None:
    root = _project(tmp_path, nested_parent=True)
    _write(root / "src" / "data" / "Repository.cs", "namespace Acme.Data;\n")
    _write(root / "src" / "app" / "App.cs", "using Acme.Data;\nclass App {}\n")

    approved, observation = _observe(root)

    assert len(observation.facts) == 1
    fact = observation.facts[0]
    assert fact.source == "shell"
    assert fact.target == "data"
    assert len(fact.provenance) == 1
    assert compare_architecture(approved, (observation,)).findings == ()


def test_invalid_utf8_and_dynamic_powershell_imports_fail_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    invalid = root / "src" / "app" / "invalid.py"
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_bytes(b"import acme.data\n\xff")
    with pytest.raises(ArchitectureDriftError, match="valid UTF-8"):
        _observe(root)

    invalid.unlink()
    _write(root / "src" / "app" / "dynamic.ps1", "Import-Module $ModuleName\n")
    with pytest.raises(ArchitectureDriftError, match="dynamic PowerShell"):
        _observe(root)


def test_python_repository_module_without_prefix_resolves_by_declared_roots(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root / "src" / "data" / "localrepo.py", "value = 1\n")
    _write(root / "src" / "app" / "consumer.py", "import localrepo\n")

    _, observation = _observe(root)

    assert any(item.source == "app" and item.target == "data" for item in observation.facts)
