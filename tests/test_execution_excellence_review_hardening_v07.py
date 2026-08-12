from __future__ import annotations

from pathlib import Path
import shutil

import pytest
import yaml

from sdai.execution_excellence import (
    EXECUTION_EXCELLENCE_SKILLS,
    ExecutionExcellenceError,
    load_execution_excellence_pack,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _copy_fixture(root: Path) -> None:
    source = _repo_root()
    for name in EXECUTION_EXCELLENCE_SKILLS:
        src = source / ".agents" / "skills" / name
        dst = root / ".agents" / "skills" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
    manifest = root / ".sdai" / "extensions" / "packs" / "sdai-execution-excellence.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        source / ".sdai" / "extensions" / "packs" / "sdai-execution-excellence.yaml",
        manifest,
    )
    for relative in (
        Path("examples/workflows/execution-excellence.yaml"),
        Path("examples/policies/execution-excellence.yaml"),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)


def test_pack_validator_uses_runtime_workflow_parser_for_invalid_mode(tmp_path: Path) -> None:
    _copy_fixture(tmp_path)
    path = tmp_path / "examples" / "workflows" / "execution-excellence.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["steps"][1]["mode"] = "invalid"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        ExecutionExcellenceError,
        match="SDAI-EXEC-004.*current workflow engine.*mode",
    ):
        load_execution_excellence_pack(tmp_path)


def test_pack_validator_uses_runtime_workflow_parser_for_missing_capability(tmp_path: Path) -> None:
    _copy_fixture(tmp_path)
    path = tmp_path / "examples" / "workflows" / "execution-excellence.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    del payload["steps"][1]["capability"]
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        ExecutionExcellenceError,
        match="SDAI-EXEC-004.*current workflow engine.*capability",
    ):
        load_execution_excellence_pack(tmp_path)


def test_pack_validator_rejects_unknown_policy_capability(tmp_path: Path) -> None:
    _copy_fixture(tmp_path)
    path = tmp_path / "examples" / "policies" / "execution-excellence.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["skills"]["required"]["deploy"] = ["verification-before-completion"]
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        ExecutionExcellenceError,
        match="SDAI-EXEC-004.*unsupported capability 'deploy'",
    ):
        load_execution_excellence_pack(tmp_path)
