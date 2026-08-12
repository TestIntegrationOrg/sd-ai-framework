from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from sdai.text import read_utf8_text


class ConfigError(RuntimeError):
    pass


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Configuration not found: {path}")
    data = yaml.safe_load(read_utf8_text(path)) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Expected mapping in {path}")
    return data
