from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Mapping

from sdai.agent_platform.context_plan import ContextPlan
from sdai.agent_platform.models import Capability, ExecutionMode
from sdai.agent_platform.runtime import AgentRuntime


CONTEXT_EXPLAIN_API_VERSION = "sdai.context-explain/v1"
TokenEstimator = Callable[[str], int]


class ContextExplainError(RuntimeError):
    """Raised when context explanation cannot be produced safely."""


def _fail(code: str, message: str) -> ContextExplainError:
    return ContextExplainError(f"{code}: {message}")


def _sha256_text(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


@dataclass(frozen=True)
class TextSizeMetric:
    chars: int
    utf8_bytes: int
    sha256: str

    @classmethod
    def from_text(cls, value: str) -> "TextSizeMetric":
        encoded = value.encode("utf-8")
        return cls(chars=len(value), utf8_bytes=len(encoded), sha256=_sha256_text(value))

    def as_dict(self) -> dict[str, object]:
        return {
            "chars": self.chars,
            "utf8Bytes": self.utf8_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class TokenEstimate:
    available: bool
    values: Mapping[str, int]
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "available": self.available,
            "components": dict(sorted(self.values.items())),
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True)
class ContextExplanation:
    feature_id: str
    capability: Capability
    mode: ExecutionMode
    profile: str
    provider: str
    agent_name: str | None
    plan: ContextPlan
    metrics: Mapping[str, TextSizeMetric]
    token_estimate: TokenEstimate

    def _body(self) -> dict[str, object]:
        return {
            "apiVersion": CONTEXT_EXPLAIN_API_VERSION,
            "featureId": self.feature_id,
            "capability": self.capability.value,
            "mode": self.mode.value,
            "workspace": self.plan.workspace,
            "profile": self.profile,
            "provider": self.provider,
            "agent": self.agent_name,
            "contextPlan": self.plan.as_dict(),
            "metrics": {
                name: metric.as_dict()
                for name, metric in sorted(self.metrics.items())
            },
            "tokenEstimate": self.token_estimate.as_dict(),
        }

    @property
    def sha256(self) -> str:
        return "sha256:" + sha256(_canonical_json(self._body()).encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        result = self._body()
        result["reportSha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"


def _estimate_tokens(
    texts: Mapping[str, str],
    estimator: TokenEstimator | None,
) -> TokenEstimate:
    if estimator is None:
        return TokenEstimate(
            available=False,
            values={},
            reason="provider-tokenizer-not-configured",
        )
    values: dict[str, int] = {}
    for name, text in sorted(texts.items()):
        try:
            count = estimator(text)
        except Exception as exc:
            raise _fail(
                "SDAI-CONTEXT-EXPLAIN-003",
                f"token estimator failed for {name}: {type(exc).__name__}",
            ) from exc
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise _fail(
                "SDAI-CONTEXT-EXPLAIN-003",
                f"token estimator returned invalid count for {name}",
            )
        values[name] = count
    return TokenEstimate(available=True, values=values)


def build_context_explanation(
    project_root: Path,
    feature_id: str,
    capability: Capability,
    *,
    profile_name: str | None = None,
    agent_name: str | None = None,
    mode: ExecutionMode = ExecutionMode.ADVISORY,
    token_estimator: TokenEstimator | None = None,
) -> ContextExplanation:
    """Explain the exact current context/prompt composition without provider execution.

    Raw prompt/context/skill/governance text is used only in-memory for deterministic
    size/hash calculation. The returned report contains hashes, sizes, paths, reason
    codes and selection metadata but never embeds those raw values.
    """
    root = project_root.resolve()
    runtime = AgentRuntime(root)
    plan = runtime.build_context_plan(
        feature_id,
        capability,
        profile_name=profile_name,
        agent_name=agent_name,
        mode=mode,
    )
    feature_context = plan.render_feature_context(root)
    governance_context = plan.render_governance_context(root)
    skills_context = plan.render_skills(root)
    invocation = runtime.build_invocation_from_context_plan(
        plan,
        profile_name=profile_name,
        agent_name=agent_name,
        mode=mode,
    )

    combined_prompt = invocation.system + "\n\n" + invocation.prompt
    texts: dict[str, str] = {
        "featureContext": feature_context,
        "governanceContext": governance_context,
        "skillsContext": skills_context,
        "systemPrompt": invocation.system,
        "taskPrompt": invocation.prompt,
        "combinedPrompt": combined_prompt,
    }
    metrics = {name: TextSizeMetric.from_text(text) for name, text in texts.items()}
    return ContextExplanation(
        feature_id=feature_id,
        capability=capability,
        mode=mode,
        profile=invocation.profile.name,
        provider=invocation.profile.provider,
        agent_name=invocation.agent_name,
        plan=plan,
        metrics=metrics,
        token_estimate=_estimate_tokens(texts, token_estimator),
    )


__all__ = [
    "CONTEXT_EXPLAIN_API_VERSION",
    "ContextExplainError",
    "ContextExplanation",
    "TextSizeMetric",
    "TokenEstimate",
    "TokenEstimator",
    "build_context_explanation",
]
