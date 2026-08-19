from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

from sdai.agent_platform.context_plan import ContextPlanError
from sdai.agent_platform.models import Capability, ExecutionMode
from sdai.context_explain import (
    CONTEXT_EXPLAIN_API_VERSION,
    ContextExplainError,
    ContextExplanation,
    build_context_explanation,
)


CONTEXT_EXPLAIN_ERROR_API_VERSION = "sdai.context-explain-error/v1"
_EXIT_OK = 0
_EXIT_INPUT = 4
_EXIT_INTEGRITY = 5


class ContextCliError(RuntimeError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ContextCliError(f"SDAI-CONTEXT-CLI-001: {message}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _error_parts(exc: BaseException, *, fallback: str) -> tuple[str, str]:
    text = str(exc)
    prefix, separator, detail = text.partition(":")
    if separator and prefix.startswith("SDAI-"):
        return prefix, detail.strip()
    return fallback, text


def _error_payload(code: str, category: str, message: str) -> dict[str, object]:
    body: dict[str, object] = {
        "apiVersion": CONTEXT_EXPLAIN_ERROR_API_VERSION,
        "category": category,
        "error": {"code": code, "message": message},
    }
    body["errorSha256"] = "sha256:" + sha256(
        _canonical_json(body).encode("utf-8")
    ).hexdigest()
    return body


def _emit_error(
    exc: BaseException,
    *,
    json_mode: bool,
    category: str,
    exit_code: int,
    fallback: str,
) -> int:
    code, message = _error_parts(exc, fallback=fallback)
    if json_mode:
        print(_canonical_json(_error_payload(code, category, message)))
    else:
        print(f"{code}: {message}", file=sys.stderr)
    return exit_code


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="sdai context explain",
        description="Explain deterministic agent context selection and prompt size without invoking a provider",
    )
    parser.add_argument("feature")
    parser.add_argument(
        "--capability",
        choices=[item.value for item in Capability],
        default=Capability.CODING.value,
        help="agent capability to explain (default: coding)",
    )
    parser.add_argument("--profile")
    parser.add_argument("--agent")
    parser.add_argument(
        "--mode",
        choices=[item.value for item in ExecutionMode],
        default=ExecutionMode.ADVISORY.value,
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path")
    return parser


def _print_human(explanation: ContextExplanation) -> None:
    plan = explanation.plan
    print(
        f"Context {explanation.feature_id} capability={explanation.capability.value} "
        f"workspace={plan.workspace}"
    )
    print(
        f"  profile={explanation.profile} provider={explanation.provider} "
        f"mode={explanation.mode.value} agent={explanation.agent_name or '-'}"
    )
    print(f"  plan_sha256={plan.sha256}")
    print(f"  report_sha256={explanation.sha256}")
    print("  metrics:")
    for name, metric in sorted(explanation.metrics.items()):
        print(
            f"    {name}: chars={metric.chars} utf8_bytes={metric.utf8_bytes} "
            f"sha256={metric.sha256}"
        )

    print(f"  selected_context={len(plan.files)}")
    for item in plan.files[:20]:
        reasons = ",".join(item.reasons)
        truncated = " truncated" if item.truncated else ""
        print(
            f"    [{item.category}] {item.source} bytes={item.size_bytes} "
            f"chars={item.chars} selected_chars={item.selected_chars}{truncated} "
            f"reasons={reasons}"
        )
    if len(plan.files) > 20:
        print(f"    ... {len(plan.files) - 20} additional selected context file(s)")

    print(f"  skills={len(plan.skills)}")
    for item in plan.skills[:20]:
        status = "selected" if item.selected else f"excluded:{item.exclusion_reason}"
        print(
            f"    {item.name}: {status} reasons={','.join(item.reasons)} "
            f"sha256={item.sha256}"
        )
    if len(plan.skills) > 20:
        print(f"    ... {len(plan.skills) - 20} additional skill decision(s)")

    print(f"  exclusions={len(plan.exclusions)}")
    for item in plan.exclusions[:20]:
        print(f"    [{item.category}] {item.source} reason={item.reason}")
    if len(plan.exclusions) > 20:
        print(f"    ... {len(plan.exclusions) - 20} additional exclusion(s)")

    if plan.diagnostics:
        print(f"  diagnostics={','.join(plan.diagnostics)}")
    token = explanation.token_estimate
    if token.available:
        values = ", ".join(f"{key}={value}" for key, value in sorted(token.values.items()))
        print(f"  token_estimate={values}")
    else:
        print(f"  token_estimate=unavailable reason={token.reason}")


def main(argv: list[str] | None = None) -> int:
    effective = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in effective
    try:
        args = _parser().parse_args(effective)
        root = Path(args.path or ".").resolve()
        explanation = build_context_explanation(
            root,
            args.feature,
            Capability(args.capability),
            profile_name=args.profile,
            agent_name=args.agent,
            mode=ExecutionMode(args.mode),
        )
    except ContextCliError as exc:
        return _emit_error(
            exc,
            json_mode=json_mode,
            category="input",
            exit_code=_EXIT_INPUT,
            fallback="SDAI-CONTEXT-CLI-001",
        )
    except ContextPlanError as exc:
        return _emit_error(
            exc,
            json_mode=json_mode,
            category="integrity",
            exit_code=_EXIT_INTEGRITY,
            fallback="SDAI-CONTEXT-PLAN-001",
        )
    except ContextExplainError as exc:
        return _emit_error(
            exc,
            json_mode=json_mode,
            category="explain",
            exit_code=_EXIT_INPUT,
            fallback="SDAI-CONTEXT-EXPLAIN-001",
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return _emit_error(
            exc,
            json_mode=json_mode,
            category="input",
            exit_code=_EXIT_INPUT,
            fallback="SDAI-CONTEXT-CLI-002",
        )

    if args.json:
        print(explanation.to_json(), end="")
    else:
        _print_human(explanation)
    return _EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONTEXT_EXPLAIN_API_VERSION",
    "CONTEXT_EXPLAIN_ERROR_API_VERSION",
    "main",
]
