from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

from sdai.audit_contracts import AuditProvenanceError
from sdai.audit_export import AuditExportError, build_audit_export_package
from sdai.audit_sinks import AuditSinkError, LocalFilesystemAuditSink, handoff_audit_export


AUDIT_EXPORT_RESULT_API_VERSION = "sdai.audit-export-result/v1"
AUDIT_EXPORT_ERROR_API_VERSION = "sdai.audit-export-error/v1"


class AuditExportCliError(RuntimeError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AuditExportCliError(f"SDAI-AUDIT-EXPORT-CLI-001: {message}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _error_parts(exc: BaseException, *, fallback: str) -> tuple[str, str]:
    text = str(exc)
    prefix, separator, detail = text.partition(":")
    if separator and prefix.startswith("SDAI-"):
        return prefix, detail.strip()
    return fallback, text


def _error_payload(exc: BaseException, *, category: str) -> dict[str, object]:
    code, message = _error_parts(exc, fallback="SDAI-AUDIT-EXPORT-CLI-002")
    body: dict[str, object] = {
        "apiVersion": AUDIT_EXPORT_ERROR_API_VERSION,
        "category": category,
        "error": {"code": code, "message": message},
    }
    body["errorSha256"] = _hash_json(body)
    return body


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="sdai audit export",
        description="Build and hand off a deterministic immutable audit export package",
    )
    parser.add_argument("feature")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--chunk-bytes", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path")
    return parser


def _result(package, receipt) -> dict[str, object]:
    body: dict[str, object] = {
        "apiVersion": AUDIT_EXPORT_RESULT_API_VERSION,
        "featureId": package.manifest.feature_id,
        "status": receipt.status,
        "sinkId": receipt.sink_id,
        "exportId": package.manifest.export_id,
        "eventCount": package.manifest.event_count,
        "ledgerHeadSha256": package.manifest.ledger_head_sha256,
        "exportSha256": package.manifest.export_sha256,
        "manifestSha256": package.manifest.manifest_sha256,
        "byteLength": package.manifest.byte_length,
        "chunkCount": len(package.manifest.chunks),
        "receiptId": receipt.receipt_id,
        "receiptSha256": receipt.receipt_sha256,
    }
    body["resultSha256"] = _hash_json(body)
    return body


def main(argv: list[str] | None = None) -> int:
    effective = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in effective
    try:
        args = _parser().parse_args(effective)
        root = Path(args.path or ".").resolve()
        kwargs = {} if args.chunk_bytes is None else {"chunk_size": args.chunk_bytes}
        package = build_audit_export_package(root, args.feature, **kwargs)
        sink = LocalFilesystemAuditSink(Path(args.destination))
        receipt = handoff_audit_export(root, args.feature, sink, package=package)
        result = _result(package, receipt)
    except AuditExportCliError as exc:
        if json_mode:
            print(_canonical_json(_error_payload(exc, category="input")))
        else:
            print(str(exc), file=sys.stderr)
        return 4
    except (AuditProvenanceError, AuditExportError, AuditSinkError) as exc:
        if json_mode:
            print(_canonical_json(_error_payload(exc, category="integrity")))
        else:
            print(str(exc), file=sys.stderr)
        return 5

    if args.json:
        print(_canonical_json(result))
    else:
        print(
            f"Audit export {result['featureId']} status={result['status']} "
            f"events={result['eventCount']} chunks={result['chunkCount']} sink={result['sinkId']}"
        )
        print(f"  export_id={result['exportId']}")
        print(f"  ledger_head={result['ledgerHeadSha256']}")
        print(f"  export_sha256={result['exportSha256']}")
        print(f"  manifest_sha256={result['manifestSha256']}")
        print(f"  receipt_id={result['receiptId']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUDIT_EXPORT_ERROR_API_VERSION",
    "AUDIT_EXPORT_RESULT_API_VERSION",
    "main",
]
