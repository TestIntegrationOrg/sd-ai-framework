from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

from sdai.audit_contracts import AuditProvenanceError
from sdai.audit_report import AuditReport, AuditReportError, AuditSelectors, build_audit_report


AUDIT_ERROR_API_VERSION = "sdai.audit-error/v1"
_EXIT_OK = 0
_EXIT_GAPS = 2
_EXIT_NO_EVENTS = 3
_EXIT_INPUT = 4
_EXIT_INTEGRITY = 5
_EVENT_CATEGORIES = frozenset({"human", "ai", "system", "workflow", "authority", "evidence"})
_ACTOR_KINDS = frozenset({"human", "ai", "system", "workflow"})
_ACTION = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")


class AuditCliError(RuntimeError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AuditCliError(f"SDAI-AUDIT-CLI-001: {message}")


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _error_payload(code: str, category: str, message: str) -> dict[str, object]:
    body: dict[str, object] = {
        "apiVersion": AUDIT_ERROR_API_VERSION,
        "category": category,
        "error": {"code": code, "message": message},
    }
    body["errorSha256"] = "sha256:" + sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    return body


def _error_parts(exc: BaseException, *, fallback: str) -> tuple[str, str]:
    text = str(exc)
    prefix, separator, detail = text.partition(":")
    if separator and prefix.startswith("SDAI-"):
        return prefix, detail.strip()
    return fallback, text


def _bounded_selector(value: str | None, *, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    if value != value.strip() or not value or len(value.encode("utf-8")) > maximum:
        raise AuditCliError(
            f"SDAI-AUDIT-CLI-002: {label} must be non-empty bounded text without surrounding whitespace"
        )
    if any(ord(char) < 32 for char in value) or "\x7f" in value:
        raise AuditCliError(f"SDAI-AUDIT-CLI-002: {label} contains control characters")
    return value


def _selectors(args: argparse.Namespace) -> AuditSelectors:
    category = _bounded_selector(args.category, label="category", maximum=32)
    actor_kind = _bounded_selector(args.actor_kind, label="actor-kind", maximum=32)
    action = _bounded_selector(args.action, label="action", maximum=128)
    if category is not None and category not in _EVENT_CATEGORIES:
        raise AuditCliError(f"SDAI-AUDIT-CLI-002: unsupported category: {category}")
    if actor_kind is not None and actor_kind not in _ACTOR_KINDS:
        raise AuditCliError(f"SDAI-AUDIT-CLI-002: unsupported actor-kind: {actor_kind}")
    if action is not None and _ACTION.fullmatch(action) is None:
        raise AuditCliError(f"SDAI-AUDIT-CLI-002: invalid action selector: {action}")
    return AuditSelectors(
        category=category,
        actor_kind=actor_kind,
        action=action,
        run_id=_bounded_selector(args.run_id, label="run", maximum=256),
        workflow=_bounded_selector(args.workflow, label="workflow", maximum=256),
        step_id=_bounded_selector(args.step_id, label="step", maximum=256),
        task_id=_bounded_selector(args.task_id, label="task", maximum=256),
        binding=_bounded_selector(args.binding, label="binding", maximum=512),
        status=_bounded_selector(args.status, label="status", maximum=128),
    )


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="sdai audit",
        description="Verify and inspect the tamper-evident audit/provenance ledger",
    )
    parser.add_argument("feature")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--category")
    parser.add_argument("--actor-kind")
    parser.add_argument("--action")
    parser.add_argument("--run", dest="run_id")
    parser.add_argument("--workflow")
    parser.add_argument("--step", dest="step_id")
    parser.add_argument("--task", dest="task_id")
    parser.add_argument("--binding")
    parser.add_argument("--status")
    parser.add_argument("--path")
    return parser


def _execution_text(event: dict[str, object]) -> str:
    execution = event.get("execution")
    if not isinstance(execution, dict):
        return ""
    values: list[str] = []
    for key, label in (("runId", "run"), ("workflow", "workflow"), ("stepId", "step"), ("taskId", "task")):
        value = execution.get(key)
        if isinstance(value, str):
            values.append(f"{label}={value}")
    return " ".join(values)


def _print_human(report: AuditReport) -> None:
    body = report.body
    print(
        f"Audit {body['featureId']} status={body['status']} "
        f"events={body['eventCount']} selected={body['selectedCount']} returned={body['returnedCount']}"
    )
    print(f"  source={body['auditSource']}")
    print(f"  ledger_head={body['ledgerHeadSha256']}")
    print(f"  export_sha256={body['exportSha256']}")
    relationships = body.get("relationships")
    if isinstance(relationships, dict):
        gaps = relationships.get("gaps")
        gap_count = len(gaps) if isinstance(gaps, list) else 0
        print(
            f"  provenance scope={relationships.get('scope')} "
            f"linked={relationships.get('linkedReferences', 0)} gaps={gap_count}"
        )
        if relationships.get("available") is False:
            print(f"  provenance unavailable code={relationships.get('errorCode')}")
        if isinstance(gaps, list):
            for gap in gaps[:10]:
                if not isinstance(gap, dict):
                    continue
                print(
                    f"  GAP {gap.get('kind')} target={gap.get('target')} "
                    f"source={gap.get('source')}:{gap.get('line')}"
                )
            if len(gaps) > 10:
                print(f"  ... {len(gaps) - 10} additional provenance gap(s)")

    events = body.get("events")
    if not isinstance(events, list):
        return
    for event in events[:20]:
        if not isinstance(event, dict):
            continue
        status = event.get("status") or "-"
        execution = _execution_text(event)
        suffix = f" {execution}" if execution else ""
        print(
            f"  #{event.get('sequence')} {event.get('category')} "
            f"actor={event.get('actorKind')} action={event.get('action')} status={status}{suffix}"
        )
    if len(events) > 20 or body.get("truncated") is True:
        selected = int(body.get("selectedCount") or 0)
        shown = min(20, len(events))
        print(f"  ... {max(0, selected - shown)} additional selected event(s)")


def _emit_error(exc: BaseException, *, json_mode: bool, category: str, exit_code: int) -> int:
    code, message = _error_parts(exc, fallback="SDAI-AUDIT-CLI-003")
    if json_mode:
        print(_canonical_json(_error_payload(code, category, message)))
    else:
        print(f"{code}: {message}", file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    effective = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in effective
    try:
        args = _parser().parse_args(effective)
        selectors = _selectors(args)
        root = Path(args.path or ".").resolve()
        report = build_audit_report(root, args.feature, selectors=selectors)
    except AuditCliError as exc:
        return _emit_error(exc, json_mode=json_mode, category="input", exit_code=_EXIT_INPUT)
    except AuditProvenanceError as exc:
        return _emit_error(exc, json_mode=json_mode, category="integrity", exit_code=_EXIT_INTEGRITY)
    except AuditReportError as exc:
        code, _ = _error_parts(exc, fallback="SDAI-AUDIT-REPORT-001")
        exit_code = _EXIT_INTEGRITY if code == "SDAI-AUDIT-REPORT-004" else _EXIT_INPUT
        return _emit_error(exc, json_mode=json_mode, category="report", exit_code=exit_code)

    if args.json:
        print(report.to_json(), end="")
    else:
        _print_human(report)
    if report.exit_code in {_EXIT_OK, _EXIT_GAPS, _EXIT_NO_EVENTS}:
        return report.exit_code
    return _EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["AUDIT_ERROR_API_VERSION", "main"]
