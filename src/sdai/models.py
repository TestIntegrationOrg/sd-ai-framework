from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class LifecycleMode(StrEnum):
    LIGHT = "light"
    STANDARD = "standard"
    CRITICAL = "critical"


@dataclass(frozen=True)
class FeatureContext:
    project_root: Path
    feature_id: str

    @property
    def feature_dir(self) -> Path:
        return self.project_root / "specs" / self.feature_id

    def artifact(self, relative_path: str) -> Path:
        return self.feature_dir / relative_path
