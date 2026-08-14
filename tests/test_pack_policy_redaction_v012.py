from __future__ import annotations

import pytest

from sdai.pack_trust_policy import (
    PACK_TRUST_POLICY_API_VERSION,
    PackTrustPolicy,
    PackTrustPolicyError,
)


def test_invalid_catalog_policy_source_does_not_echo_embedded_credentials() -> None:
    raw = {
        "apiVersion": PACK_TRUST_POLICY_API_VERSION,
        "requireSignatures": True,
        "allowedCatalogs": ["https://user:secret@example.com/catalog"],
        "deniedCatalogs": [],
        "allowedPublishers": ["*"],
        "deniedPublishers": [],
    }

    with pytest.raises(PackTrustPolicyError) as captured:
        PackTrustPolicy.from_dict(raw)

    message = str(captured.value)
    assert "SDAI-PACK-POLICY-001" in message
    assert "invalid catalog source policy value" in message
    assert "user" not in message
    assert "secret" not in message
    assert "example.com" not in message
