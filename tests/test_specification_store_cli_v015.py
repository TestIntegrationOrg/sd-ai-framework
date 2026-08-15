from __future__ import annotations

import json
from pathlib import Path

from sdai.cli import main


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".sdai").mkdir(parents=True)
    (root / ".sdai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    return root


def _json_line(capsys) -> dict[str, object]:
    output = capsys.readouterr().out
    assert output.endswith("\n")
    assert output.count("\n") == 1
    return json.loads(output)


def test_store_cli_create_register_list_doctor_and_context_json(
    tmp_path: Path,
    capsys,
) -> None:
    project = _project(tmp_path)
    store = tmp_path / "external-store"

    assert main(
        [
            "store",
            "create",
            "platform-specs",
            "--version",
            "1.0.0",
            "--destination",
            str(store),
            "--json",
        ]
    ) == 0
    created = _json_line(capsys)
    assert created["identity"] == "platform-specs@1.0.0"
    assert created["created"] is True

    assert main(
        ["store", "register", str(store), "--path", str(project), "--json"]
    ) == 0
    registered = _json_line(capsys)
    assert registered["registered"] is True
    assert registered["pathScope"] == "external"

    assert main(["store", "list", "--path", str(project), "--json"]) == 0
    listing_text = capsys.readouterr().out
    listing = json.loads(listing_text)
    assert listing["stores"][0]["identity"] == "platform-specs@1.0.0"
    assert str(store.resolve()) not in listing_text

    assert main(["store", "doctor", "--path", str(project), "--json"]) == 0
    doctor = _json_line(capsys)
    assert doctor["healthy"] is True
    assert doctor["storeCount"] == 1

    assert main(
        [
            "store",
            "context",
            "--store",
            "platform-specs",
            "--version",
            "1.0.0",
            "--path",
            str(project),
            "--json",
        ]
    ) == 0
    context_text = capsys.readouterr().out
    context = json.loads(context_text)
    assert context["stores"][0]["identity"] == "platform-specs@1.0.0"
    assert str(store.resolve()) not in context_text


def test_store_cli_repeat_create_and_register_are_idempotent(
    tmp_path: Path,
    capsys,
) -> None:
    project = _project(tmp_path)
    store = tmp_path / "store"
    create_args = [
        "store",
        "create",
        "platform-specs",
        "--version",
        "1.0.0",
        "--destination",
        str(store),
        "--json",
    ]
    assert main(create_args) == 0
    _json_line(capsys)
    assert main(create_args) == 0
    repeated_create = _json_line(capsys)
    assert repeated_create["created"] is False

    register_args = ["store", "register", str(store), "--path", str(project), "--json"]
    assert main(register_args) == 0
    _json_line(capsys)
    assert main(register_args) == 0
    repeated_register = _json_line(capsys)
    assert repeated_register["registered"] is False


def test_store_cli_doctor_uses_unhealthy_exit_class(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    (project / ".sdai" / "specification-stores.yaml").write_text(
        "invalid: declaration\n",
        encoding="utf-8",
    )

    assert main(["store", "doctor", "--path", str(project), "--json"]) == 2
    result = _json_line(capsys)
    assert result["healthy"] is False
    assert result["findings"][0]["code"] == "SDAI-STORE-DOCTOR-002"
