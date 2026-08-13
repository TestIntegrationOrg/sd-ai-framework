from __future__ import annotations

from enum import Enum


COMPLETION_POLICY_API_VERSION = "sdai.completion-policy/v1"
SPEC_REVIEW_CONTRACT = "sdai.completion/spec-review/v1"
CODE_QUALITY_REVIEW_CONTRACT = "sdai.completion/code-quality-review/v1"
FINAL_REVIEW_CONTRACT = "sdai.completion/final-review/v1"
TEST_CONTRACT = "sdai.completion/test/v1"
QUALITY_CONTRACT = "sdai.completion/quality/v1"
SECURITY_CONTRACT = "sdai.completion/security/v1"
APPROVAL_CONTRACT = "sdai.completion/approval/v1"
VERIFICATION_CONTRACT = "sdai.completion/verification/v1"


class CompletionRisk(str, Enum):
    TRIVIAL = "trivial"
    STANDARD = "standard"
    CRITICAL = "critical"
    REGULATED = "regulated"


class CompletionScope(str, Enum):
    TASK = "task"
    CHANGE = "change"
