from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys

from sdai import __version__
from sdai.architecture_cli import main as architecture_main
from sdai.architecture_trace_cli import main as trace_main
from sdai.audit_cli import main as audit_main
from sdai.audit_export_cli import main as audit_export_main
from sdai.context_cli import main as context_main
from sdai.contract_cli import main as contract_main
from sdai.converge_cli import main as converge_main
from sdai.entrypoint import main as lifecycle_main
from sdai.feature_graph_cli import main as feature_graph_main
from sdai.multi_repo_run_cli import main as multi_repo_run_main
from sdai.multi_repo_verify_cli import main as multi_repo_verify_main
from sdai.trace_policy_cli import main as trace_policy_main
from sdai.verify_cli import main as verify_main
from sdai.versioning import write_framework_metadata
from sdai.workflow_audit_cli import (
    run_lifecycle_with_workflow_audit,
    run_multi_repo_with_workflow_audit,
)


def _project_root(argv: list[str]) -> Path:
    for index, value in enumerate(argv):
        if value == "--path" and index + 1 < len(argv):
            return Path(argv[index + 1]).resolve()
        if value.startswith("--path="):
            return Path(value.split("=", 1)[1]).resolve()
    return Path(".").resolve()


def _run_trace(argv: list[str]) -> int:
    if argv and argv[0] == "policy":
        return trace_policy_main(argv[1:])

    # `TraceGraph.to_json()` already includes its canonical trailing newline.
    # The trace module currently uses print() for export, so normalize only the
    # versioned executable boundary to avoid adding a second newline. This keeps
    # `sdai trace export ...` byte-for-byte equal to the canonical graph JSON.
    if argv and argv[0] == "export":
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = trace_main(argv)
        output = buffer.getvalue()
        if output.endswith("\n\n"):
            output = output[:-1]
        sys.stdout.write(output)
        return result
    return trace_main(argv)


def _is_multi_repo_run(argv: list[str]) -> bool:
    return any(
        value in {"--repo", "--all", "--plan"} or value.startswith("--repo=")
        for value in argv
    )


def main(argv: list[str] | None = None) -> int:
    effective = list(sys.argv[1:] if argv is None else argv)
    if effective in (["--version"], ["-V"]):
        print(f"sdai {__version__}")
        return 0

    # Keep the legacy `sdai feature FEATURE --title ... --description ...`
    # lifecycle parser unchanged. The nested graph surface is dispatched before
    # lifecycle parsing so existing required intake arguments remain intact.
    if len(effective) >= 2 and effective[0] == "feature" and effective[1] == "graph":
        return feature_graph_main(effective[2:])

    # Only the nested drift surface is intercepted. Legacy
    # `sdai architecture FEATURE ...` lifecycle/artifact validation remains owned
    # by the original lifecycle parser below.
    if len(effective) >= 2 and effective[0] == "architecture" and effective[1] == "drift":
        return architecture_main(effective[2:])

    # Context explanation is provider-free/read-only and owns its nested grammar.
    # Dispatch only `context explain` so future/legacy `context` commands are not
    # accidentally consumed by this surface.
    if len(effective) >= 2 and effective[0] == "context" and effective[1] == "explain":
        return context_main(effective[2:])

    # Audit export is a nested write-to-retention surface with its own stable parser,
    # error contract, and integrity exits. Dispatch it before read-only audit reporting
    # so `sdai audit export FEATURE ...` is never interpreted as feature `export`.
    if len(effective) >= 2 and effective[0] == "audit" and effective[1] == "export":
        return audit_export_main(effective[2:])

    # Audit reporting is a read-only versioned surface over the existing ledger and
    # canonical trace projection. Dispatch it before the legacy lifecycle parser so
    # no existing command grammar or orchestration behavior changes.
    if effective and effective[0] == "audit":
        return audit_main(effective[1:])

    # Multi-repository execution keeps its existing deterministic plan/authority
    # surface; only the local Orchestrator binding is replaced while it runs so the
    # same feature-scoped workflow audit is produced in each repository.
    if effective and effective[0] == "run" and _is_multi_repo_run(effective[1:]):
        return run_multi_repo_with_workflow_audit(multi_repo_run_main, effective[1:])

    if effective and effective[0] == "trace":
        return _run_trace(effective[1:])

    if effective and effective[0] == "verify":
        if "--all-repos" in effective[1:]:
            return multi_repo_verify_main(effective[1:])
        return verify_main(effective[1:])

    if effective and effective[0] == "converge":
        return converge_main(effective[1:])

    if effective and effective[0] == "contract":
        return contract_main(effective[1:])

    # Preserve the legacy parser/output exactly. The temporary module hook changes
    # only its Orchestrator class during this call, so `run` and `step run` gain
    # workflow/policy provenance without a parallel CLI implementation.
    result = run_lifecycle_with_workflow_audit(lifecycle_main, effective)
    if result == 0 and effective and effective[0] in {"init", "upgrade"}:
        root = _project_root(effective)
        metadata = write_framework_metadata(root)
        print(f"SD-AI framework version {__version__}")
        print(f"  + {metadata.relative_to(root).as_posix()}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
