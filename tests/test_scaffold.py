from pathlib import Path

from sdai.scaffold import init_project


def test_init_project_creates_expected_files(tmp_path: Path):
    init_project(tmp_path)
    assert (tmp_path / ".sdai" / "config.yaml").exists()
    assert (tmp_path / ".sdai" / "workflows" / "standard.yaml").exists()
    assert (tmp_path / "specs").is_dir()
