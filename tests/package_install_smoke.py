from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="strict",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def _venv_paths(root: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        return root / "Scripts" / "python.exe", root / "Scripts" / "sdai.exe"
    return root / "bin" / "python", root / "bin" / "sdai"


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    clean_env = dict(os.environ)
    clean_env.pop("PYTHONPATH", None)
    clean_env["PYTHONUTF8"] = "1"

    with tempfile.TemporaryDirectory(prefix="sdai-package-smoke-") as raw_temp:
        temp = Path(raw_temp)
        wheelhouse = temp / "wheelhouse"
        wheelhouse.mkdir()

        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--wheel-dir",
                str(wheelhouse),
                str(repo),
            ],
            cwd=temp,
            env=clean_env,
        )
        project_wheels = sorted(wheelhouse.glob("sd_ai_framework-*.whl"))
        if len(project_wheels) != 1:
            raise AssertionError(
                f"expected exactly one sd-ai-framework wheel, found {project_wheels}"
            )

        venv_root = temp / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_root)
        python_exe, sdai_exe = _venv_paths(venv_root)
        _run(
            [
                str(python_exe),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                str(project_wheels[0]),
            ],
            cwd=temp,
            env=clean_env,
        )

        version = _run([str(sdai_exe), "--version"], cwd=temp, env=clean_env)
        if not version.stdout.startswith("sdai ") or version.stderr:
            raise AssertionError(
                f"installed console entrypoint failed: stdout={version.stdout!r} "
                f"stderr={version.stderr!r}"
            )

        brownfield = temp / "existing-application"
        brownfield.mkdir()
        application = brownfield / "service.txt"
        application.write_text("existing application bytes\n", encoding="utf-8")

        initialized = _run(
            [str(sdai_exe), "init", "--path", str(brownfield)],
            cwd=temp,
            env=clean_env,
        )
        if "Initialized SD-AI project" not in initialized.stdout:
            raise AssertionError(initialized.stdout)
        if application.read_text(encoding="utf-8") != "existing application bytes\n":
            raise AssertionError("sdai init modified brownfield application content")

        local_owned = brownfield / ".sdai" / "local-team.yaml"
        local_owned.write_text("owner: package-smoke\n", encoding="utf-8")
        metadata = brownfield / ".sdai" / "framework-version.yaml"
        if not metadata.is_file():
            raise AssertionError("sdai init did not install framework metadata")
        metadata.unlink()

        migrated = _run(
            [
                str(sdai_exe),
                "migrate",
                "apply",
                "--json",
                "--path",
                str(brownfield),
            ],
            cwd=temp,
            env=clean_env,
        )
        payload = json.loads(migrated.stdout)
        if payload.get("status") != "applied" or not payload.get("migrationId"):
            raise AssertionError(f"unexpected migration result: {payload}")
        if not metadata.is_file():
            raise AssertionError("packaged migration did not restore missing managed metadata")
        if local_owned.read_text(encoding="utf-8") != "owner: package-smoke\n":
            raise AssertionError("packaged migration modified local-owned SDAI content")
        if application.read_text(encoding="utf-8") != "existing application bytes\n":
            raise AssertionError("packaged migration modified application content")

        current = _run(
            [str(sdai_exe), "upgrade", "--path", str(brownfield)],
            cwd=temp,
            env=clean_env,
        )
        if "already has the current scaffold" not in current.stdout:
            raise AssertionError(f"upgrade is not idempotent: {current.stdout!r}")

        rolled_back = _run(
            [
                str(sdai_exe),
                "migrate",
                "rollback",
                str(payload["migrationId"]),
                "--json",
                "--path",
                str(brownfield),
            ],
            cwd=temp,
            env=clean_env,
        )
        rollback_payload = json.loads(rolled_back.stdout)
        if rollback_payload.get("status") != "rolled-back":
            raise AssertionError(f"unexpected rollback result: {rollback_payload}")
        if metadata.exists():
            raise AssertionError("rollback did not restore the pre-migration missing-file state")
        if local_owned.read_text(encoding="utf-8") != "owner: package-smoke\n":
            raise AssertionError("rollback modified local-owned SDAI content")
        if application.read_text(encoding="utf-8") != "existing application bytes\n":
            raise AssertionError("rollback modified application content")

    print("SDAI wheel install/bootstrap/upgrade/rollback smoke: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
