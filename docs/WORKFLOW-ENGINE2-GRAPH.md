# Workflow Engine 2 — Canonical Graph Contract

SDAI 0.14 introduces a canonical Workflow Engine 2 graph without replacing the legacy workflow executor in the first slice. `sdai.workflow-graph/v2` is the deterministic structural contract; later 0.14 slices add safe executable leaves, layered registry provenance, overlay v2, durable execution/resume, and CLI surfaces on top of the same graph.

## Versioning

Existing workflow files remain valid:

- unversioned and legacy workflows continue to use the existing leaf-step syntax;
- typed inputs and workflow components retain their v6 minimum;
- plugin leaves retain their v8 minimum;
- new Workflow Engine 2 control nodes require workflow `version: 9` or newer.

Legacy `parallel` remains graph-readable at older versions under its existing advisory-agent restrictions. Using the new `max_concurrency` field or composing the new v9 controls opts into Workflow Engine 2 semantics.

## Control-flow nodes

Workflow Engine 2 recognizes these structural node types:

| Type | Required bounded fields | Child shape |
|---|---|---|
| `sequence` | — | `steps` |
| `if` | safe `condition` | `then`, optional `else` |
| `switch` | safe `value` | `cases[].when/steps`, optional `default` |
| `parallel` | `max_concurrency` for v9 usage | `steps` |
| `fan-out` | `max_items`, `max_concurrency` | `items`, `as`, `steps` |
| `fan-in` | finite unique `sources` | no children; aggregation `strategy` |
| `foreach` | `max_items` | `items`, `as`, `steps` |
| `bounded-while` | `max_iterations` | `condition`, `steps` |

Current hard limits are intentionally conservative:

- direct control children: 100;
- fan-out/foreach items: 100;
- bounded-while iterations: 100;
- parallel/fan-out concurrency: 32.

Missing or invalid bounds fail validation. A literal collection whose size already exceeds `max_items` also fails before execution.

## Non-code expression language

Control conditions and selectors use finite JSON expressions. SDAI never calls Python `eval`, a shell, Jinja, or dynamic attribute evaluation.

Supported operators are:

```yaml
# references
{ref: inputs.release}
{exists: steps.review.status}

# comparisons / membership
{eq:  [{ref: inputs.channel}, prod]}
{ne:  [left, right]}
{lt:  [{ref: loop.iteration}, 3]}
{lte: [left, right]}
{gt:  [left, right]}
{gte: [left, right]}
{in:  [api, {ref: inputs.targets}]}
{not-in: [legacy, {ref: inputs.targets}]}

# boolean composition
{and: [EXPR, EXPR]}
{or:  [EXPR, EXPR]}
{not: EXPR}

# explicit structured literal
{literal: [api, web]}
```

Bare JSON scalars/lists are literals. A mapping is treated as an expression operator and must have exactly one supported key; structured object literals therefore use `literal`. References are restricted to dotted `inputs`, `steps`, `item`, and `loop` namespaces. Expression depth and logical term counts are bounded.

`evaluate_workflow_expression(...)` is a pure deterministic evaluator over an explicit context mapping. Shell metacharacters, Python-looking strings, and Unicode are data, not executable syntax.

## Canonical paths

User step IDs remain globally unique for backward compatibility. Canonical node paths add structural scope:

```text
$root
sequence-a/child
release-if/$then/review
release-if/$else/skip
route/$case/0/prod
route/$default/fallback
fan/$body/review-item
loop/$body/recheck
```

Segments beginning with `$` are framework-reserved **virtual scopes**, not user step IDs or executable nodes. They preserve branch/body parentage without inventing runnable steps. Node records carry their canonical `parent` scope and ordered child paths; `if`/`switch` also expose explicit branch records.

Graph edges have deterministic meanings:

- `contains`: structural scope/parent membership; a source may be a documented virtual scope;
- `next`: deterministic order between sibling executable/control nodes;
- `branch`: control node to the first real node of an `if`/`switch` branch;
- `body`: foreach/fan-out/bounded-while node to the first real body node;
- `fan-in`: resolved real source node to the fan-in node.

Fan-in sources may use an exact canonical path or a globally unique step ID. Missing/ambiguous sources fail closed; self-dependency is rejected.

## Canonical JSON and hashes

`load_workflow_graph(...)` first reuses the existing SDAI pipeline:

1. workflow inheritance and organization/repo/user overlays;
2. typed input resolution;
3. workflow component expansion;
4. Workflow Engine 2 graph validation/canonicalization.

It returns `sdai.workflow-resolution/v2`, which embeds:

- workflow name/version and validation mode;
- typed input definitions and resolved public input metadata;
- component provenance;
- existing inheritance/overlay/lifecycle-hook/mandatory-step provenance;
- `graphSha256`;
- the canonical `sdai.workflow-graph/v2` graph.

Both resolution and graph use compact, sorted, finite UTF-8 JSON and expose SHA-256 digests. Sensitive typed input values are not serialized; their public representation is marked sensitive and hashed/redacted by the existing typed-input boundary.

## Leaf compatibility

This slice delegates legacy leaf parsing to the existing workflow contracts so these remain compatible:

- deterministic;
- agent;
- approval;
- validate;
- quality-gate;
- plugin.

Plugin input **values** are not emitted into canonical graph JSON. The node exposes the plugin ID, sorted input keys, and an input-value SHA-256 binding instead.

`safe-command` is intentionally not added here; it belongs to #164 so executable leaf security can be reviewed independently from structural graph parsing.

## Examples

```yaml
version: 9
name: release
validation_mode: standard
inputs:
  release:
    type: boolean
    required: true
  targets:
    type: string-list
    default: [api, web]
steps:
  - id: decide
    type: if
    condition: {eq: [{ref: inputs.release}, true]}
    then:
      - id: approved
        type: approval
        gate: release
    else:
      - id: validate-only
        type: validate

  - id: review-targets
    type: fan-out
    items: {ref: inputs.targets}
    as: target
    max_items: 10
    max_concurrency: 4
    steps:
      - id: review-target
        type: validate

  - id: converge
    type: fan-in
    sources: [decide, review-targets]
    strategy: all-success
```

The graph contract validates and explains this structure only. Execution ordering, branch expansion, checkpoints, fan-in result aggregation, approvals, cancellation, and resume are implemented in later Workflow Engine 2 slices rather than hidden inside parsing.
