# Current Specifications and Typed Change Model (0.7 foundation)

Issue #55 introduces the **read-only data model and parser** for SDAI's brownfield/current-state specification lifecycle. It intentionally does **not** implement promotion or any write path into `specs/current`.

## Source-of-truth layout

```text
specs/
├── current/
│   └── <domain>/
│       └── specification.md
└── changes/
    └── <FEATURE>/
        ├── change.yaml
        └── deltas/
            └── <domain>.yaml
```

`specs/current/<domain>/specification.md` is canonical current truth. Change documents are proposals only. Later promotion logic must validate and apply them through the deterministic SDAI engine.

## Current specification identity

`load_current_spec()` reads UTF-8, normalizes line endings through SDAI's shared text boundary, and returns:

- domain
- portable repository-relative source path
- normalized content
- `sha256:<hex>` identity

The normalized hash makes the same specification identity stable across Windows and Linux line-ending differences.

## `change.yaml`

```yaml
version: 1
feature_id: SIGN-123
title: Add governed signing behavior
description: Optional human-readable context.
status: draft                # draft | proposed
domains:
  - signing
  - certificates
baselines:
  signing: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  certificates: null        # null means the change expects a new domain
```

Rules:

- feature/domain identifiers use safe portable grammars;
- domains are unique and normalized to deterministic order;
- `baselines` must contain exactly one key for every declared domain;
- baseline values are either `sha256:<64 lowercase hex>` or `null`;
- unknown fields fail rather than being silently ignored;
- status is intentionally limited to authoring states (`draft`, `proposed`). Approval/promotion state belongs to deterministic lifecycle evidence, not an AI-editable source field.

## Delta document

```yaml
version: 1
domain: signing
baseline_spec_sha256: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
operations:
  - op: ADDED
    requirement_id: FR-004
    definition: The service MUST validate a trusted timestamp.
    reason: New signing requirement.

  - op: MODIFIED
    requirement_id: FR-001
    previous_hash: sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    definition: The service MUST sign PowerShell input and preserve UTF-8 metadata.
    reason: Clarify observable behavior.

  - op: REMOVED
    requirement_id: FR-002
    previous_hash: sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
    reason: Superseded behavior.

  - op: RENAMED
    requirement_id: FR-003
    new_requirement_id: FR-005
    previous_hash: sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
    reason: Align the requirement taxonomy.
```

### Operation contracts

| Operation | Required | Forbidden |
|---|---|---|
| ADDED | `requirement_id`, `definition`, `reason` | `previous_hash`, `new_requirement_id` |
| MODIFIED | `requirement_id`, `previous_hash`, `definition`, `reason` | `new_requirement_id` |
| REMOVED | `requirement_id`, `previous_hash`, `reason` | `definition`, `new_requirement_id` |
| RENAMED | `requirement_id`, `new_requirement_id`, `previous_hash`, `reason` | `definition` |

`MODIFIED`, `REMOVED`, and `RENAMED` therefore cannot even parse without prior requirement identity evidence. Whether the supplied hash still matches current truth is validated in #56.

A single delta document may not contain multiple operations targeting the same `requirement_id`; that is structurally ambiguous and fails before semantic validation.

## Bundle consistency

`load_spec_change()` loads `change.yaml` and every YAML delta in deterministic filename order, then enforces:

- every delta domain is declared by the change;
- exactly one delta exists for every declared domain;
- delta baseline and `change.yaml` baseline match exactly;
- all source paths stay inside the repository through the shared path-safety boundary.

The resulting `SpecChangeBundle` has a stable JSON representation using repository-relative POSIX paths and UTF-8 content.

## Error codes

| Code | Meaning |
|---|---|
| `SDAI-SPEC-001` | unsafe/invalid portable identifier |
| `SDAI-SPEC-002` | missing/unreadable/invalid UTF-8 or YAML source |
| `SDAI-SPEC-003` | invalid document schema or unknown field |
| `SDAI-SPEC-004` | invalid operation type/field contract |
| `SDAI-SPEC-005` | missing/invalid SHA-256 identity evidence |
| `SDAI-SPEC-006` | ambiguous duplicate operation target |
| `SDAI-SPEC-007` | inconsistent change/delta bundle |

Path escapes continue to use SDAI's shared `PathSafetyError` contract so the new spec model does not fork the framework's security semantics.

## Trust boundary

This module has no function that writes or promotes canonical current truth. Its responsibilities are:

```text
resolve paths → read UTF-8 → parse strictly → type operations → hash → serialize evidence
```

Conflict detection and baseline validation are #56. Semantic diff and atomic promotion are #57. AI agents may author proposal files, but they cannot use this model to directly update `specs/current`.
