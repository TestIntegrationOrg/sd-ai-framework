from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SensitiveFinding:
    code: str
    message: str


_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "PRIVATE_KEY",
        "Private key material detected in agent context",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "AWS_ACCESS_KEY",
        "AWS access key identifier detected in agent context",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "GITHUB_TOKEN",
        "GitHub token-like value detected in agent context",
        re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    ),
)


def find_sensitive_content(text: str) -> list[SensitiveFinding]:
    findings: list[SensitiveFinding] = []
    for code, message, pattern in _PATTERNS:
        if pattern.search(text):
            findings.append(SensitiveFinding(code, message))
    return findings


def enforce_prompt_safety(system: str, prompt: str) -> None:
    findings = find_sensitive_content(system + "\n" + prompt)
    if findings:
        summary = "; ".join(f"{item.code}: {item.message}" for item in findings)
        raise RuntimeError(
            "External agent invocation blocked by SD-AI prompt-safety policy. "
            f"Remove or redact sensitive material first. {summary}"
        )
