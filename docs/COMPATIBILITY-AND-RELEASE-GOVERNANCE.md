# SDAI 1.x Compatibility and Release Governance

This policy defines the public compatibility promise and release decision
process for stable SDAI 1.x. It is the canonical governance policy for package
changes; subsystem documents remain authoritative for their own versioned
contracts and security invariants.

The policy is identity-independent. Its roles and evidence describe release
responsibilities, not verified human identity or distinct-person approval.

## Stable 1.x compatibility surface

| Surface | 1.x promise | Authority |
|---|---|---|
| Package identity | `sd-ai-framework` version is sourced only from `src/sdai/__init__.py::__version__`; wheel metadata, `sdai --version`, README release metadata, and scaffold framework metadata agree | [Release workflow](RELEASING.md) |
| Supported runtime | Python 3.11 and 3.12 on Ubuntu, Windows, and macOS remain release-tested | [Platform confidence](PLATFORM-CONFIDENCE.md) |
| Extension boundary | `sdai/v1`, stable `sdai.extensions` exports, registry precedence/locks, and compatibility-sensitive `SDAI-EXT-*`/`SDAI-REG-*` meanings remain compatible | [Stable extensions](EXTENSIONS.md) |
| Automation JSON | Cataloged API identities, field names/types/meanings, discriminators, and machine-clean `--json` stdout follow the 1.x rule | [JSON contracts](JSON-CONTRACTS.md) |
| Migration and scaffold safety | Managed-file ownership, preview/apply/rollback contracts, preservation, hash binding, recovery, and fail-closed ambiguity remain compatible | [Migrations](MIGRATIONS.md) |
| Registries and policy | Documented layer order, authoritative built-in/organization locks, provenance, and monotonic non-weakening rules remain stable | [Enterprise policy](ENTERPRISE-POLICY.md) |
| CLI | Documented command/option behavior explicitly covered by compatibility tests remains available; cataloged JSON is the automation interface | [README](../README.md) |
| Security boundary | Path containment, safe parsing, explicit argv execution, permission intersection, policy authority, and evidence freshness cannot be silently weakened | [Execution security](EXECUTION-SECURITY.md) |

Stable does not mean frozen. SDAI 1.x may add commands, optional fields,
extension kinds, integrations, and policy controls when existing consumers can
continue safely and the owning contract is updated.

## Not a public compatibility promise

Unless another stable contract explicitly says otherwise, these are internal:

- non-exported Python modules, functions, dataclasses, and underscore-prefixed
  names;
- temporary files, caches, internal checkpoints, transaction implementation
  details, and non-cataloged evidence payloads;
- human-readable progress text, log wording, whitespace, and display ordering;
- provider-native generated files that are documented as managed derivatives;
- test helpers and historical release-evidence document layout;
- experimental or prerelease surfaces explicitly labeled unstable.

Internal changes still must preserve security, user-owned content, and any
public behavior they feed. Labeling an implementation detail internal is not a
way to bypass a stable external contract.

## Package semantic versioning

SDAI uses SemVer for the package version:

| Change | Package version | Examples |
|---|---|---|
| Patch | `1.x.PATCH` | Compatible defect fix, security hardening, documentation/evidence correction, performance improvement with unchanged semantics |
| Minor | `1.MINOR.0` | Backward-compatible capability, command, optional contract field, extension kind, or announced deprecation |
| Major | `MAJOR.0.0` | Removal or incompatible change to a stable surface, supported-platform removal, or intentional authority/semantic break |

Prerelease identifiers may be used for candidate evaluation but do not weaken
the eventual stable release gate.

The package version is independent from manifest, Pack, workflow, schema,
protocol, evidence, and JSON `apiVersion` values. An owning contract changes its
version only when its own semantics require it. A package release must not
mechanically rewrite unrelated version fields.

## Change classification

| Change | Compatible in 1.x? | Required treatment |
|---|---|---|
| Add a new surface | Yes | Document, test, assign an explicit contract identity when machine-facing |
| Add an optional JSON field | Yes, only when existing consumers may safely ignore it | Preserve all existing fields/meaning and update catalog tests/docs |
| Add a stricter organization policy option | Yes | Default compatibly; lower layers cannot weaken the effective policy |
| Fix behavior that contradicts documented semantics | Usually | Add regression evidence and call out observable corrections |
| Remove/rename/change type or meaning of a stable field | No under the same identity | Add a versioned successor and migration guidance |
| Remove a stable Python export, manifest kind, registry layer, or lock guarantee | No in 1.x | Deprecate; remove only in the next major unless the security exception applies |
| Drop a supported Python/OS matrix leg | No in 1.x | Treat as a major compatibility decision with migration/support notice |
| Weaken containment, policy, permission, evidence, or authority checks | No | Prohibited; a major version does not excuse an undocumented security weakening |

A breaking machine contract must never be shipped by silently editing the
meaning of an existing API identifier. Introduce a successor, support an
explicit transition period, and preserve deterministic failure for ambiguous
old/new inputs.

## Deprecation policy

A deprecation must include all of the following:

1. the first package version in which it is deprecated;
2. the stable surface and affected consumers;
3. the supported replacement and migration steps;
4. the earliest major version in which removal may occur;
5. tests that keep the deprecated path working through the supported 1.x line.

Deprecation and removal do not occur in the same stable release. A deprecated
1.x surface remains supported for the rest of 1.x and may be removed in the
next major release. Diagnostic warnings must not corrupt cataloged JSON stdout;
use documentation, changelog entries, or a contract-safe diagnostic channel.

## Security and compliance exception

SDAI may reject behavior in a patch release when continuing to accept it would
violate a documented security, policy-authority, data-integrity, or platform
safety invariant. This exception is for tightening an unsafe boundary, not for
ordinary API redesign.

An exception requires:

- a written threat, vulnerability, or compliance rationale without publishing
  secrets or exploit-enabling detail;
- the narrowest safe change and explicit affected-version scope;
- regression tests proving both the unsafe case is blocked and normal
  compatible behavior remains available;
- migration or remediation guidance when a safe alternative exists;
- a security advisory/changelog/release-note entry appropriate to disclosure
  timing;
- the normal frozen-head, review-thread, full-suite, and wheel-install gates.

Emergency work may shorten scheduling and coordination, but it cannot bypass CI,
overwrite user-owned content, weaken higher-layer authority, claim
unverified identity, or reuse an unchanged machine-contract identity for an
incompatible payload shape.

## Release responsibilities

The process uses four responsibilities:

- **scope owner** — defines included issues, explicit exclusions, and blockers;
- **implementer** — produces code/docs/tests and focused evidence;
- **reviewer** — checks the exact diff, acceptance criteria, compatibility,
  security, and actionable review threads;
- **release coordinator** — freezes the candidate SHA, records matrix evidence,
  and makes the go/no-go recommendation.

One account may perform multiple responsibilities in SDAI 1.0. The durable
issue, pull request, commit SHA, review threads, tests, and CI runs are the
identity-independent evidence. These roles do not assert GitHub Enterprise,
OIDC, SSO, cryptographic signer, or distinct-approver identity.

## Release states and blockers

| State | Meaning |
|---|---|
| Planned | Scope and dependencies are recorded; implementation may change |
| Candidate | Scope and head SHA are frozen for final review and matrix validation |
| Go | Acceptance criteria pass, no open P0/P1 blocker, reviews are resolved, and exact-head CI is green |
| No-go | Any required evidence is missing, stale, failed, ambiguous, or bound to another SHA |
| Merged | The reviewed candidate was merged using the approved method |
| Verified main | The exact squash-merged `main` SHA passed its distinct six-leg `push: main` CI run |
| Published | A separately authorized tag/package publication completed and was verified |
| Held | Scope is explicitly deferred and excluded from release claims |

Release blockers include a failing/absent matrix leg, filtered test suite,
failed wheel smoke, unresolved actionable review thread, version/contract drift,
unsafe migration behavior, stale evidence, an unreviewed head change, or a
claim that includes held capability.

## Candidate and release procedure

1. Confirm every included issue is complete and every exclusion is explicit.
2. Verify compatibility classification, migrations, documentation, and release
   notes against this policy.
3. Run focused tests, then `python -m pytest -q` and
   `python tests/package_install_smoke.py` locally.
4. Open one focused pull request from current `main`; review the complete diff.
5. Address findings and freeze the final head SHA. Any new commit invalidates prior exact-head evidence and restarts final validation.
6. Require zero unresolved actionable review threads.
7. Require the unfiltered suite and isolated wheel smoke to pass on Ubuntu, Windows, and macOS with Python 3.11 and 3.12 for the exact frozen SHA.
8. Record the frozen candidate SHA, pull-request CI run, and all six candidate
   job results, then merge only that head using the repository's approved merge
   method.
9. Resolve the distinct squash-merged `main` SHA and require its `push: main` CI
   run to pass the same unfiltered suite and wheel smoke on all six legs.
   PR-head evidence cannot prove the merged SHA.
10. Record the merged-main SHA, `push: main` CI run, and all six merged-main job
    results. A missing or failed merged-main run is a no-go for publication.
11. Confirm `main` contains the intended merge and no unrelated release change.
12. Create a tag or publish a package only through a separate explicit action
    after merged-main evidence is green; verify the published version and digest.

A green run for an earlier commit cannot authorize a changed head. Local tests
support review but do not replace the required GitHub matrix.

## Release record and recovery

The release record must identify the package version, included slices,
exclusions/held scope, frozen candidate SHA and PR CI run/job results, distinct
squash-merged `main` SHA and `push: main` CI run/job results, supported matrix,
known limitations, and publication status. Historical evidence remains
immutable history rather than being rewritten to match later releases.

If a merged release candidate is defective, prefer a forward patch or a
documented revert that passes the same gate. Do not force-push `main`, silently
replace a tag/package, delete migration evidence, or claim rollback succeeded
without current verification.

## Explicit held scope

The 0.18/#25 identity-backed approvals remain held and are not part of SDAI
1.0. This policy does not implement or claim GitHub Enterprise/OIDC/SSO
identity verification, cryptographic approver signatures/timestamps,
identity-backed authorization, or distinct-approver enforcement. Resuming that
scope requires explicit authorization, its own versioned contracts, threat
model, migration plan, and release evidence.
