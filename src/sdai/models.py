from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re

from sdai.path_safety import ensure_within_project


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
        path = self.project_root / "specs" / self.feature_id
        return ensure_within_project(
            self.project_root, path, label=f"feature workspace '{self.feature_id}'"
        )

    def artifact(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("artifact path must stay inside the feature workspace")
        path = self.feature_dir / candidate
        # Check both boundaries. The project-level check rejects a symlinked specs/
        # tree that escapes the repository; the feature-level check keeps one feature
        # from addressing another feature through an internal symlink.
        ensure_within_project(
            self.project_root, path, label="feature artifact path"
        )
        return ensure_within_project(
            self.feature_dir, path, label="feature artifact path"
        )
