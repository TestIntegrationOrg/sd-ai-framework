from __future__ import annotations

import pytest

from sdai.agent_platform import ProviderFailureCategory, ProviderRetryError, RetryPolicy
from sdai.agent_platform.provider_retry import _safe_retry_id


def test_retry_id_reserves_space_for_diagnostic_attempt_suffix() -> None:
    allowed = "r" * 123
    assert _safe_retry_id(allowed) == allowed
    assert len(f"{allowed}-a001") == 128
    with pytest.raises(ProviderRetryError, match="at most 123"):
        _safe_retry_id("r" * 124)


def test_retry_policy_requires_typed_unique_categories() -> None:
    with pytest.raises(ValueError, match="ProviderFailureCategory"):
        RetryPolicy(retryable_categories=("timeout",))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicates"):
        RetryPolicy(
            retryable_categories=(
                ProviderFailureCategory.TIMEOUT,
                ProviderFailureCategory.TIMEOUT,
            )
        )
