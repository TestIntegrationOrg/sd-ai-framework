from __future__ import annotations

import argparse
import json
from pathlib import Path

from sdai.spec_promotion import (
    build_spec_diff,
    preview_promotion,
    promote_spec_change,
    record_promotion_approval,
)
from sdai.spec_validation import validate_spec_change


SPEC_VALIDATION_API_VERSION = "sdai.spec-validation/v1"
SPEC_DIFF_API_VERSION = "sdai.spec-diff/v1"
SPEC_PROMOTION_APPROVAL_API_VERSION = "sdai.spec-promotion-approval/v1"
SPEC_PROMOTION_PREVIEW_API_VERSION = "sdai.spec-promotion-preview/v1"
SPEC_PROMOTION_RESULT_API_VERSION = "sdai.spec-promotion-result/v1"


def add_spec_parser(commands: argparse._SubParsersAction) -> None:
    spec = commands.add_parser(
        "spec",
        help="Validate, diff, approve, and promote current specification changes",
    )
    actions = spec.add_subparsers(dest="spec_action", required=True)

    validate = actions.add_parser("validate", help="Validate a typed spec change")
    validate.add_argument("feature")
    validate.add_argument("--json", action="store_true")
    validate.add_argument("--path")

    diff = actions.add_parser("diff", help="Render a semantic current-spec diff")
    diff.add_argument("feature")
    diff.add_argument("--json", action="store_true")
    diff.add_argument(
        "--include-content",
        action="store_true",
        help="Include complete proposed current-spec content in JSON output",
    )
    diff.add_argument("--path")

    approve = actions.add_parser(
        "approve",
        help="Record a hash-bound approval for specification promotion",
    )
    approve.add_argument("feature")
    approve.add_argument("--by", required=True, dest="approved_by")
    approve.add_argument("--role", default="")
    approve.add_argument("--note", default="")
    approve.add_argument("--json", action="store_true")
    approve.add_argument("--path")

    promote = actions.add_parser(
        "promote",
        help="Promote a validated, approved change into current truth",
    )
    promote.add_argument("feature")
    promote.add_argument("--dry-run", action="store_true")
    promote.add_argument("--json", action="store_true")
    promote.add_argument(
        "--include-content",
        action="store_true",
        help="Include complete proposed current-spec content in dry-run JSON output",
    )
    promote.add_argument("--path")


def _versioned_json(value: dict[str, object], api_version: str) -> str:
    payload = {"apiVersion": api_version, **value}
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def _print_validation(feature: str, report) -> None:
    print(
        f"Spec validation feature={feature} valid={str(report.valid).lower()} "
        f"change_sha256={report.change_sha256} findings={len(report.findings)}"
    )
    for finding in report.findings:
        target = f" requirement={finding.requirement_id}" if finding.requirement_id else ""
        print(
            f"  {finding.severity.upper():5} {finding.code} {finding.kind} "
            f"domain={finding.domain}{target}: {finding.message}"
        )


def _print_diff(report) -> None:
    print(
        f"Spec diff feature={report.feature_id} change_sha256={report.change_sha256} "
        f"domains={len(report.domains)} parallel_conflicts={len(report.parallel_conflicts.findings)}"
    )
    for domain in report.domains:
        print(
            f"  domain={domain.domain} before={domain.before_sha256 or '<absent>'} "
            f"after={domain.after_sha256} source={domain.source}"
        )
        for change in domain.changes:
            destination = (
                f" -> {change.new_requirement_id}" if change.new_requirement_id else ""
            )
            section = f" section={change.section}" if change.section else ""
            print(
                f"    {change.op:8} {change.requirement_id}{destination}{section} "
                f"reason={change.reason}"
            )
    for finding in report.parallel_conflicts.findings:
        print(
            f"  WARNING {finding.code} {finding.kind} domain={finding.domain} "
            f"requirement={finding.requirement_id}: {finding.message}"
        )


def run_spec_command(root: Path, args: argparse.Namespace) -> int:
    if args.spec_action == "validate":
        report = validate_spec_change(root, args.feature)
        if args.json:
            print(_versioned_json(report.as_dict(), SPEC_VALIDATION_API_VERSION))
        else:
            _print_validation(args.feature, report)
        return 0 if report.valid else 1

    if args.spec_action == "diff":
        report = build_spec_diff(root, args.feature)
        if args.json:
            print(
                _versioned_json(
                    report.as_dict(include_content=args.include_content),
                    SPEC_DIFF_API_VERSION,
                )
            )
        else:
            _print_diff(report)
        return 0

    if args.spec_action == "approve":
        decision = record_promotion_approval(
            root,
            args.feature,
            approved_by=args.approved_by,
            role=args.role,
            note=args.note,
        )
        if args.json:
            print(
                _versioned_json(
                    decision.as_dict(),
                    SPEC_PROMOTION_APPROVAL_API_VERSION,
                )
            )
        else:
            print(
                f"Spec promotion approval feature={args.feature} "
                f"gate={decision.gate} satisfied={str(decision.satisfied).lower()} "
                f"approvals={decision.approvals}/{decision.required} "
                f"stale={str(decision.stale).lower()} detail={decision.detail}"
            )
        return 0 if decision.satisfied else 3

    if args.spec_action == "promote":
        if args.dry_run:
            preview = preview_promotion(root, args.feature)
            if args.json:
                print(
                    _versioned_json(
                        preview.as_dict(include_content=args.include_content),
                        SPEC_PROMOTION_PREVIEW_API_VERSION,
                    )
                )
            else:
                print(
                    f"Spec promotion dry-run feature={args.feature} "
                    f"eligible={str(preview.eligible).lower()} "
                    f"approval={str(preview.approval.satisfied).lower()}"
                )
                _print_diff(preview.diff)
            # Dry-run is intentionally usable before approval so reviewers can
            # inspect the exact proposed truth transition before approving it.
            return 0

        result = promote_spec_change(root, args.feature)
        if args.json:
            print(_versioned_json(result.as_dict(), SPEC_PROMOTION_RESULT_API_VERSION))
        else:
            print(
                f"Promoted specification change feature={result.feature_id} "
                f"promotion_id={result.promotion_id} archive={result.archive_path}"
            )
            for domain, digest in sorted(result.after_sha256.items()):
                print(f"  current domain={domain} sha256={digest}")
        return 0

    raise ValueError(f"Unknown spec action: {args.spec_action}")
