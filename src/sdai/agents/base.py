from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from sdai.models import FeatureContext


@dataclass(frozen=True)
class AgentResult:
    agent: str
    artifacts: list[Path]
    summary: str


class Agent(ABC):
    name: str

    @abstractmethod
    def run(self, context: FeatureContext) -> AgentResult:
        raise NotImplementedError
