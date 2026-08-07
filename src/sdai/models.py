from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re


class LifecycleMode(StrEnum):
    LIGHT = "light"
    STANDARD = "standard"
    CRITICAL = "critical"


_SAFE_FEATURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_feature_id(value: str) -> str:
    value = value.strip()
    if not _SAFE_FEATURE_ID.fullmatch(value):
        raise ValueError(
            "feature_id must use only letters, numbers, dot, underscore, or hyphen"
        )
    return value


@dataclass(frozen=True)
class FeatureContext:
    project_root: Path
    feature_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_root", self.project_root.resolve())
        object.__setattr__(self, "feature_id", validate_feature_id(self.feature_id))

    @property
    def feature_dir(self) -> Path:
        return self.project_root / "specs" / self.feature_id

    def artifact(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("artifact path must stay inside the feature workspace")
        return self.feature_dir / candidate
