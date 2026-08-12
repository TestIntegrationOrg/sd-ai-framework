# Minimal Compatible Skill Resolution

SDAI 0.7 resolves reusable expertise independently from semantic role and AI provider identity.

```text
Semantic role
+ lifecycle capability
+ task/domain context
+ detected repository technology
+ effective enterprise/repository/user policy
        ↓
Minimal Skill Resolver
        ↓
compatible dependency-ordered skill set
        ↓
provider-neutral skill instructions
        ↓
Provider Router / execution runtime
```

The resolver does **not** create `java-developer`, `dotnet-architect`, `codex-java-reviewer`, or other technology/provider-specific semantic agents. `developer`, `architect`, `code-reviewer`, `tester`, `security-reviewer`, and other semantic roles remain provider-neutral.

## Command

```bash
sdai skill resolve \
  --agent developer \
  --capability coding \
  --task "implement AWS KMS signing" \
  --domain code-signing

sdai skill resolve \
  --agent developer \
  --capability coding \
  --skill secure-coding \
  --json
```

`--skill` is repeatable and means the caller explicitly requires that skill for this resolution. Explicit requests are subject to the same role, capability, dependency, technology, and version checks as agent- or policy-required skills.

## Skill metadata

Canonical skill instructions remain in:

```text
.agents/skills/<skill>/SKILL.md
```

Resolver metadata extends the existing `sdai.yaml` sidecar without replacing the canonical skill format:

```yaml
version: 1
capabilities: [coding, review]
compatible_agents: [developer, code-reviewer]
requires:
  - java-engineering
compatibility:
  languages:
    java: ">=17,<22"
  frameworks:
    spring-boot: ">=3,<4"
  platforms:
    aws: null
selection:
  auto: true
  roles: [developer]
  capabilities: [coding]
  task_keywords: [spring, service, api]
  domains: [services]
```

All fields except `version` are optional. Existing v0.6 sidecars such as:

```yaml
version: 1
capabilities: [coding, review, security]
```

remain valid without modification.

### `capabilities`

Restricts where the skill may be used. An explicitly required skill that does not support the requested lifecycle capability fails deterministically.

### `compatible_agents`

Restricts semantic roles. Use this only when the expertise truly does not apply to other roles. Technology should normally be expressed through `compatibility`, not by creating language-specific agents.

### `requires`

Declares skill dependencies. Dependencies are validated and emitted before the dependent skill so composed prompts are deterministic.

Missing dependencies and dependency cycles are hard failures.

### `compatibility`

Requires repository technology evidence from `sdai tech detect`.

Supported categories are the same stable categories as the technology report:

- `languages`
- `frameworks`
- `build_tools`
- `platforms`
- `libraries`
- `testing`

A null compatibility value checks only for technology presence:

```yaml
compatibility:
  platforms:
    aws: null
```

A non-null value checks a numeric version constraint:

```yaml
compatibility:
  languages:
    java: ">=17,<22"
```

Resolver v1 supports exact numeric versions and `>=`, `>`, `<=`, `<`, `==`, `=` with comma-separated clauses. It intentionally does not implement package-manager-specific grammars such as npm caret/tilde ranges.

### `selection`

Automatic selection is opt-in:

```yaml
selection:
  auto: true
```

Optional `roles`, `capabilities`, `task_keywords`, and `domains` narrow automatic selection. This keeps prompts small: technology presence alone does not cause every compatible skill in a repository to be injected.

## Selection sources and strictness

Resolver inputs fall into two groups.

### Required seeds — strict

These must resolve successfully or the command fails:

1. skills declared by the selected semantic agent;
2. skills required by effective policy for the lifecycle capability;
3. skills explicitly supplied with `--skill`.

Policy-required skills remain additive. Organization-required skills cannot be removed by repository/user customization because the existing effective-policy merge uses mandatory-skill union semantics.

### Automatic candidates — conservative

A skill with `selection.auto: true` is selected only when all configured filters and compatibility checks pass.

An incompatible automatic candidate is skipped with an explainable decision instead of failing the whole resolution. This is what allows a repository to contain Java, .NET, Python, and other reusable skills while selecting only the subset relevant to the current role/task.

## Version evidence is conservative

A versioned skill is selected only when SDAI can prove an exact numeric technology version from strong repository evidence or an explicit exact `.sdai/technology.yaml` pin.

For example, a dependency declaration such as:

```text
fastapi>=0.115
```

proves FastAPI is present and provides a lower bound, but it does **not** prove that the installed runtime version is exactly `0.115`. Resolver v1 therefore does not use that weak dependency bound as an exact version for compatibility checks.

If the organization/repository needs deterministic version compatibility, pin the technology explicitly:

```yaml
# .sdai/technology.yaml
version: 1
frameworks:
  fastapi: "0.115"
```

The same principle prevents ambiguous technology evidence from being silently coerced into a version claim.

## Minimality

The resolver does not inject every detected language/framework/platform skill. A skill reaches the final set only through:

- semantic-agent declaration;
- effective-policy requirement;
- explicit user/workflow request;
- opt-in automatic selection whose role/capability/task/domain/technology conditions all match;
- dependency of one of the above.

Each skill appears at most once in the final set.

## Explainability JSON

`sdai skill resolve --json` records:

- selected semantic role and capability;
- task/domain context;
- final dependency-ordered skill names;
- agent-declared, policy-required, and explicitly requested seeds;
- a decision for every installed skill with selected/skipped reasons and origins;
- the complete deterministic technology report used for compatibility decisions.

This evidence is provider-independent. It is suitable for later prompt/context explanation and audit work.

## Error codes

| Code | Meaning |
|---|---|
| `SDAI-SKILL-001` | invalid resolver metadata or unsupported compatibility syntax |
| `SDAI-SKILL-002` | semantic-role or lifecycle-capability incompatibility |
| `SDAI-SKILL-003` | required technology/version compatibility cannot be proven or is incompatible |
| `SDAI-SKILL-005` | skill dependency cycle |
| `SDAI-SKILL-006` | required skill/dependency is not installed |

## Current 0.7 boundary

This slice establishes deterministic metadata, compatibility checking, dependency expansion, policy union, minimal selection, prompt composition, and explainability. It does **not** yet replace every existing runtime prompt-composition path automatically.

That separation is intentional: Tier-1 language/framework packs and execution-discipline packs can first be authored and validated against a stable resolver contract, then runtime integration can be added without hard-coding language/provider behavior into the core.
