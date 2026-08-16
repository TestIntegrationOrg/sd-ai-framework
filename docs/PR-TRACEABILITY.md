# Provider-Neutral Pull Request Traceability

SD-AI 0.15.7 extends the multi-repository feature graph from requirement ownership and repository-local implementation facts to explicit pull request evidence without making a hosting provider part of canonical identity or truth.

The repository-local evidence API is:

```text
sdai.pr-evidence/v1
```

## Evidence location

Each participating repository may declare PR evidence for a feature at:

```text
specs/changes/<FEATURE>/pr-evidence.yaml
```

Example:

```yaml
apiVersion: sdai.pr-evidence/v1
kind: PullRequestEvidence
featureId: FEATURE-123
repositoryId: api
pullRequests:
  - id: review-17
    headCommit: 0123456789abcdef0123456789abcdef01234567
    state: open
    links:
      - task:TASK-API-001
      - code:path-sha256:...
      - test:path-sha256:...
      - evidence:EVIDENCE-API-001
    provider:
      name: github
      reference: "17"
      url: https://example.invalid/pulls/17
```

`id` is a repository-local portable identifier. Canonical graph identity is:

```text
pr-reference:<repository-id>:<local-id>
```

Provider name, number/reference, and URL are optional display metadata. They are included in evidence provenance/hashes when present, but they do **not** define the node ID, prove that a PR exists, or satisfy verification by themselves.

## Local authority and commit checks

SD-AI performs no provider API call while building PR traceability. For every `headCommit`, it uses only the explicitly declared local repository to verify:

1. the object exists as a local Git commit;
2. the commit is reachable from the repository's current `HEAD`.

A locally missing commit is `UNREACHABLE`. A commit that exists but is not an ancestor of current `HEAD` is `STALE`. Neither can create an `included-in-pr` edge.

This intentionally works for both open and merged evidence without requiring a network lookup. A `closed` reference is retained as provenance but does not satisfy traceability.

## Allowed links

PR evidence may link only existing repository-local trace nodes of these types:

- `task:*`
- `code:*`
- `test:*`
- `evidence:*`

The link must exist in that same repository's current feature `TraceGraph`. A link that is missing, renamed, or belongs only to another repository is stale and cannot satisfy PR traceability.

When all checks pass, the multi-repository graph adds:

```text
<task|code|test|evidence> --included-in-pr--> pr-reference:<repo>:<id>
```

Existing requirement→task/code/test/evidence trace relations and requirement/task `owned-by` repository edges remain unchanged. SD-AI does not guess missing links from branch names, commit messages, remotes, provider URLs, or PR numbers.

## Evidence provenance

Every PR node and `included-in-pr` edge is bound to:

- repository id;
- feature id;
- local PR id;
- declared and locally resolved head commit;
- evidence source path relative to the repository;
- evidence source SHA-256;
- canonical PR-evidence manifest SHA-256;
- resolved evidence SHA-256;
- exact linked trace-node identities;
- optional provider display metadata.

Local absolute repository paths are never emitted in graph JSON.

## Deterministic findings

PR evidence findings use the `SDAI-FEATURE-GRAPH-PR-EVIDENCE-*` family, including:

- `MISSING` — a feature participant has trace/ownership facts but no PR evidence;
- `INVALID` — malformed/unsafe evidence;
- `CONFLICT` — duplicate/conflicting local evidence declarations;
- `CROSS-FEATURE` — feature/repository scope does not match the participant;
- `UNREACHABLE` — the declared commit object is not available locally;
- `STALE` — the commit exists but is not reachable from current `HEAD`;
- `STALE-LINK` — a linked task/code/test/evidence node is absent from the current repository trace;
- `CLOSED` — evidence is retained but does not satisfy current traceability.

A PR node may remain in the graph for explainability when commit state is stale/closed, but no satisfying `included-in-pr` edge is created.

## Verification behavior

`sdai run FEATURE --repo/--all` continues to use the #186 base graph for execution authority. Missing PR evidence therefore does not prevent implementation work before a PR exists.

`sdai verify --all-repos --feature FEATURE`, however, builds the PR-extended graph. Any `SDAI-FEATURE-GRAPH-PR-EVIDENCE-*` finding makes aggregate verification a policy failure even when each repository's ordinary verification engine passes. This keeps PR traceability a verification requirement without turning provider metadata into execution authority.

The aggregate report carries:

- the PR-extended feature graph SHA-256;
- canonical graph findings;
- the independent #186 execution-plan SHA-256 used for participant/baseline authority;
- each repository-local verification report.

## Safety and portability

PR traceability is read-only and provider-neutral. It never clones, fetches, pulls, pushes, merges, rebases, creates PRs, or calls a provider service.

The evidence file is bounded to 1 MiB, UTF-8/UTF-8-BOM input is supported, YAML aliases and duplicate mapping keys are rejected, symlink/junction/reparse redirects are rejected, and canonical ordering is independent of YAML list order. UTF-8 provider display metadata is supported on Windows and Linux because it is never used as a filesystem or canonical identity key.