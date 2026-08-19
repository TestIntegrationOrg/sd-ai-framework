from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sdai.diagnostics import DiagnosticsError, build_diagnostics_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdai diagnostics",
        description="Inspect read-only context, routing, provider, retry and audit diagnostics.",
    )
    parser.add_argument("feature", help="Feature identifier")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit canonical JSON")
    parser.add_argument("--run", dest="run_id", help="Filter diagnostics through audit run identity")
    parser.add_argument("--task", dest="task_id", help="Filter diagnostics through audit task identity")
    parser.add_argument("--path", default=".", help="Project root (default: current directory)")
    return parser


def _error_code(error: BaseException) -> str:
    text = str(error)
    prefix = text.split(":", 1)[0].strip()
    if prefix.startswith("SDAI-") and len(prefix) <= 80:
        return prefix
    return type(error).__name__


def _human(body: dict[str, object]) -> str:
    lines = [
        f"SDAI diagnostics: {body.get('featureId')}",
        f"status: {body.get('status')}",
        f"workspace: {body.get('workspace')}",
    ]
    context = body.get("context")
    if isinstance(context, dict):
        if context.get("available"):
            metrics = context.get("metrics")
            combined = metrics.get("combinedPrompt") if isinstance(metrics, dict) else None
            lines.append(
                "context: available"
                f" capability={context.get('capability')} profile={context.get('profile')}"
                f" plan={((context.get('contextPlan') or {}).get('planSha256') if isinstance(context.get('contextPlan'), dict) else None)}"
            )
            if isinstance(combined, dict):
                lines.append(
                    f"prompt-size: chars={combined.get('chars')} utf8-bytes={combined.get('utf8Bytes')} sha256={combined.get('sha256')}"
                )
        else:
            lines.append(
                f"context: unavailable reason={context.get('reason')} error={context.get('errorType')}"
            )
    routing = body.get("routing")
    if isinstance(routing, dict):
        if routing.get("available"):
            lines.append(
                f"routing: selected={routing.get('selectedProfile')} reason={routing.get('selectionReason')} sha256={routing.get('decisionSha256')}"
            )
        else:
            lines.append(
                f"routing: unavailable reason={routing.get('reason')} sha256={routing.get('decisionSha256')}"
            )
    attempts = body.get("providerAttempts")
    if isinstance(attempts, list):
        lines.append(f"provider-attempts: {len(attempts)}")
        for attempt in attempts[-10:]:
            if not isinstance(attempt, dict):
                continue
            timing = attempt.get("timing")
            total_ns = timing.get("totalNs") if isinstance(timing, dict) else None
            failure = attempt.get("failure")
            category = failure.get("category") if isinstance(failure, dict) else None
            lines.append(
                f"  {attempt.get('attemptId')}: {attempt.get('status')}"
                f" provider={attempt.get('provider')} profile={attempt.get('profile')}"
                f" total-ns={total_ns} heartbeats={attempt.get('heartbeatCount')}"
                f" failure={category}"
            )
    retry = body.get("retryExecutions")
    if isinstance(retry, list):
        lines.append(f"retry-executions: {len(retry)}")
        for item in retry[-10:]:
            if isinstance(item, dict):
                lines.append(
                    f"  {item.get('retryId')}: {item.get('status')} attempts={item.get('attempts')} policy={item.get('policySha256')}"
                )
    audit = body.get("audit")
    if isinstance(audit, dict):
        lines.append(
            f"audit: status={audit.get('status')} selected={audit.get('selectedCount')} head={audit.get('ledgerHeadSha256')}"
        )
    partial = body.get("partialReasons")
    if isinstance(partial, list) and partial:
        lines.append("partial: " + ", ".join(str(item) for item in partial[:20]))
    lines.append(f"report-sha256: {body.get('reportSha256')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_diagnostics_report(
            Path(args.path),
            args.feature,
            run_id=args.run_id,
            task_id=args.task_id,
        )
    except Exception as exc:
        # Diagnostics may surface integrity failures from audit/context/routing layers.
        # Expose only a stable type/code here; raw exception/provider content is not
        # copied to the operator surface.
        print(f"sdai diagnostics error: {_error_code(exc)}", file=sys.stderr)
        return 2
    if args.as_json:
        sys.stdout.write(report.to_json())
    else:
        print(_human(report.to_dict()))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
