from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen


class JiraIntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class JiraIssue:
    key: str
    summary: str
    description: str
    status: str
    issue_type: str
    priority: str
    labels: tuple[str, ...]
    url: str


Transport = Callable[[Request], bytes]


def _default_transport(request: Request) -> bytes:
    with urlopen(request, timeout=30) as response:  # nosec B310 - HTTPS is enforced by JiraClient
        return response.read()


class JiraClient:
    """Minimal Jira REST adapter with credentials sourced from environment.

    Supported authentication:
      JIRA_BEARER_TOKEN
      or JIRA_EMAIL + JIRA_API_TOKEN

    Project files never contain credentials and HTTPS is mandatory.
    """

    def __init__(
        self,
        base_url: str,
        *,
        email: str | None = None,
        api_token: str | None = None,
        bearer_token: str | None = None,
        transport: Transport | None = None,
    ):
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise JiraIntegrationError("Jira base URL must use https://")
        self.base_url = base_url
        self.email = email
        self.api_token = api_token
        self.bearer_token = bearer_token
        self.transport = transport or _default_transport

    @classmethod
    def from_env(cls, *, transport: Transport | None = None) -> "JiraClient":
        base_url = os.getenv("JIRA_BASE_URL", "").strip()
        if not base_url:
            raise JiraIntegrationError("JIRA_BASE_URL is required")
        return cls(
            base_url,
            email=os.getenv("JIRA_EMAIL"),
            api_token=os.getenv("JIRA_API_TOKEN"),
            bearer_token=os.getenv("JIRA_BEARER_TOKEN"),
            transport=transport,
        )

    def _authorization(self) -> str:
        if self.bearer_token:
            return f"Bearer {self.bearer_token}"
        if self.email and self.api_token:
            raw = f"{self.email}:{self.api_token}".encode("utf-8")
            return "Basic " + base64.b64encode(raw).decode("ascii")
        raise JiraIntegrationError(
            "Configure JIRA_BEARER_TOKEN or JIRA_EMAIL + JIRA_API_TOKEN"
        )

    def _get_json(self, path: str) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            headers={
                "Accept": "application/json",
                "Authorization": self._authorization(),
                "User-Agent": "sd-ai-framework",
            },
            method="GET",
        )
        try:
            payload = self.transport(request)
        except Exception as exc:  # urllib exposes several transport exception types
            raise JiraIntegrationError(f"Jira request failed: {exc}") from exc
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JiraIntegrationError("Jira returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise JiraIntegrationError("Jira returned an unexpected response")
        return data

    def issue(self, key: str) -> JiraIssue:
        key = key.strip()
        if not key:
            raise JiraIntegrationError("Jira issue key is required")
        fields = "summary,description,status,issuetype,priority,labels"
        data = self._get_json(f"/rest/api/3/issue/{quote(key, safe='')}?fields={fields}")
        issue_fields = data.get("fields") or {}
        if not isinstance(issue_fields, dict):
            issue_fields = {}
        return JiraIssue(
            key=str(data.get("key") or key),
            summary=str(issue_fields.get("summary") or ""),
            description=_adf_to_text(issue_fields.get("description")),
            status=_nested_name(issue_fields.get("status")),
            issue_type=_nested_name(issue_fields.get("issuetype")),
            priority=_nested_name(issue_fields.get("priority")),
            labels=tuple(str(v) for v in (issue_fields.get("labels") or []) if v),
            url=f"{self.base_url}/browse/{quote(str(data.get('key') or key), safe='-')}",
        )


def _nested_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return ""


def _adf_to_text(value: Any) -> str:
    """Flatten Atlassian Document Format into readable intake text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_adf_to_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if not isinstance(value, dict):
        return str(value)

    node_type = str(value.get("type") or "")
    if node_type == "text":
        return str(value.get("text") or "")
    content = value.get("content") or []
    text = _adf_to_text(content)
    if node_type in {"paragraph", "heading", "listItem", "blockquote"}:
        return text + ("\n" if text else "")
    if node_type in {"bulletList", "orderedList", "doc"}:
        return text
    if node_type == "hardBreak":
        return "\n"
    return text


def jira_issue_intake(issue: JiraIssue, feature_id: str, workflow: str) -> str:
    labels = ", ".join(issue.labels) or "-"
    return f"""# Feature Intake — {feature_id}

## Title
{issue.summary}

## Description
{issue.description or '(no Jira description)'}

## Requested Lifecycle
{workflow}

## Source
jira

## Source Reference
- Issue: {issue.key}
- URL: {issue.url}
- Type: {issue.issue_type or '-'}
- Priority: {issue.priority or '-'}
- Status: {issue.status or '-'}
- Labels: {labels}

## Status
intake
"""
