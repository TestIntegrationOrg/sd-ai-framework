# SDAI Pack Eval and Certification Contract

SDAI 0.12.6 adds provider-neutral quality certification for an exact Pack artifact. Certification is deliberately separate from signature/trust verification: a Pack can be authentic but fail quality policy, or pass quality policy but still be untrusted by catalog/signature policy.

## Versioned contracts

The certification flow uses four canonical contracts:

- `sdai.pack-eval-suite/v1` — current eval case identities, dimensions, and exact case-input SHA-256 values.
- `sdai.pack-certification-policy/v1` — one organization/repository/user policy input.
- `sdai.pack-certification-policy-resolved/v1` — deterministic non-weakening policy composition and provenance.
- `sdai.pack-eval-evidence/v1` — scored evidence bound to exact Pack, policy, and eval-suite inputs.
- `sdai.pack-certification-decision/v1` — provider-neutral certification status and reasons.

All hashes use canonical UTF-8 JSON and `sha256:<lowercase-hex>` values.

## Eval suite

Example:

```json
{
  "apiVersion": "sdai.pack-eval-suite/v1",
  "cases": [
    {
      "id": "quality-case",
      "dimension": "quality",
      "caseSha256": "sha256:..."
    },
    {
      "id": "security-case",
      "dimension": "security",
      "caseSha256": "sha256:..."
    }
  ]
}
```

`caseSha256` represents the exact provider-independent eval input (scenario/prompt/assertion fixture or another deterministic case artifact). Cases are canonicalized by id and duplicate ids are rejected.

The suite SHA-256 therefore changes when case membership, dimension assignment, or case input changes.

## Certification policy

Example repository policy:

```json
{
  "apiVersion": "sdai.pack-certification-policy/v1",
  "requireCertification": true,
  "default": {
    "minimumScoreBasisPoints": 8500,
    "requiredDimensions": {
      "quality": 8500,
      "security": 9000
    },
    "requiredCases": ["quality-case"]
  },
  "capabilities": {
    "workflows": {
      "minimumScoreBasisPoints": 9000,
      "requiredDimensions": {
        "security": 9500
      },
      "requiredCases": ["workflow-security-case"]
    }
  },
  "risks": {
    "high": {
      "minimumScoreBasisPoints": 9500,
      "requiredDimensions": {},
      "requiredCases": ["high-risk-case"]
    }
  }
}
```

Scores are integer basis points from `0` through `10000`; `8500` means `85.00%`. Integer scoring avoids provider/platform floating-point variance.

### Non-weakening scope resolution

Organization, repository, and user policy inputs are composed in that order. A lower scope cannot weaken a stronger requirement:

- `requireCertification` uses logical OR.
- minimum scores use the maximum configured value.
- dimension thresholds use the maximum threshold for each dimension.
- required cases are unioned.
- capability/risk requirements are merged with the same rules.

The resolved policy records each input policy SHA-256 and scope. Its own SHA-256 is part of certification evidence, so any effective policy change invalidates old evidence.

## Evidence

`sdai.pack-eval-evidence/v1` binds scored results to:

- exact `publisher/id@version`,
- canonical Pack manifest SHA-256,
- canonical Pack content SHA-256,
- resolved certification policy SHA-256,
- current eval-suite SHA-256,
- exact case ids/dimensions/case hashes, and
- integer case scores/pass status.

Producer metadata (`provider`, `model`, `runner`) is recorded for traceability but excluded from `truthSha256`. A model/provider string can therefore never make failed provider-independent facts pass.

SDAI also checks that evidence covers exactly the current suite case set and that every evidence case retains the suite's dimension and case-input SHA-256. Claiming the current suite hash while changing case inputs fails closed.

## Deterministic scoring

SDAI does not compare rounded floating-point averages. A threshold is satisfied using exact integer arithmetic:

```text
sum(case score basis points) >= threshold * number of cases
```

Displayed aggregate and dimension scores use the integer floor of the corresponding average; the pass/fail comparison itself uses the exact sum expression above.

Required case failures remain failures regardless of aggregate score.

## Freshness and fail-closed status

Certification status is one of:

- `certified` — exact current Pack/content/policy/suite and all effective requirements pass.
- `failed` — current inputs are present but score, required-case, required-dimension, or suite-case integrity checks fail.
- `stale` — Pack identity, manifest, content, policy, or eval suite changed after evidence was produced.
- `missing` — policy requires certification but no evidence exists.
- `malformed` — supplied evidence cannot satisfy the evidence contract.
- `not-required` — effective policy does not require certification and no evidence is supplied.

Missing, malformed, failed, and stale certification use exit code `4` from the CLI. `certified` and `not-required` use `0`.

## CLI

```text
sdai pack certification \
  --source ./pack-artifact \
  --suite ./pack-eval-suite.json \
  --organization-policy ./org-cert-policy.json \
  --repository-policy ./repo-cert-policy.json \
  --user-policy ./user-cert-policy.json \
  --evidence ./pack-eval-evidence.json \
  --risk high \
  --json
```

Policy flags are optional. `--suite` is mandatory because freshness cannot be established without the current eval input set. `--evidence` is optional so the command can report `missing` or `not-required` deterministically.

With `--json`, stdout contains only the machine-readable decision. Validation diagnostics continue to use the top-level SDAI stderr/error convention.

## Relationship to existing eval foundations

The existing skill/agent eval runner remains responsible for executing provider/model work and producing deterministic assertion results. Pack certification is the higher-level evidence/policy layer: eval case artifacts are hashed into the Pack eval suite, normalized scores/pass facts are recorded as Pack evidence, and certification evaluates those facts without trusting provider/model metadata.

This separation permits additional eval executors while keeping the certification truth and enterprise policy behavior provider-neutral.

## Security properties

Certification fails closed when required and:

- Pack identity/version changed,
- manifest or Pack content changed,
- effective enterprise/repository/user policy changed,
- eval suite or exact case inputs changed,
- a required case is missing or failed,
- a required dimension is missing or below threshold,
- aggregate score is below threshold,
- evidence is missing or malformed.

No producer/provider/model metadata is considered when deciding these facts.