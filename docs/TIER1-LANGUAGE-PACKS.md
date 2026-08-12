# Tier-1 Language and Framework Packs

SDAI 0.7 ships the first built-in language expertise as extension assets rather than new semantic-agent classes.

```text
architect / developer / code-reviewer / tester / security-reviewer
                            +
                 detected repository technology
                            +
                  #59 Minimal Skill Resolver
                            ↓
       language skill + applicable framework skill(s)
                            ↓
                 provider-neutral instructions
```

There is no `java-developer`, `dotnet-architect`, `python-tester`, `codex-java-developer`, or equivalent provider/language role.

## Built-in Tier-1 pack manifests

Pack manifests live under `.sdai/extensions/packs/` and use the stable `sdai/v1` extension envelope.

| Pack | Core language skills | Framework examples |
|---|---|---|
| `sdai-java` | `java-engineering` | `spring-boot` |
| `sdai-dotnet` | `csharp-engineering` | `aspnet-core` |
| `sdai-python` | `python-engineering` | `fastapi`, `django` |
| `sdai-typescript-javascript` | `javascript-engineering`, `typescript-engineering` | `nodejs-engineering`, `react-engineering`, `angular-engineering` |
| `sdai-go` | `go-engineering` | — |
| `sdai-powershell` | `powershell-engineering` | — |

Example:

```yaml
apiVersion: sdai/v1
kind: Pack
metadata:
  id: sdai-java
  version: 0.1.0
  description: Tier-1 SDAI language pack for Java.
spec:
  type: language
  languages: [java]
  skills:
    core: [java-engineering]
    frameworks: [spring-boot]
```

This is deliberately a **composition manifest**. Full catalog installation, signing, provenance, locking, publisher trust, and remote update behavior remain the dedicated Pack/Catalog milestone. The 0.7 skeleton does not fake those controls.

## Skill composition

Each core skill declares language presence compatibility and opts into automatic resolution for the engineering lifecycle capabilities where language expertise is useful:

```yaml
version: 1
capabilities: [architecture, coding, review, testing, security]
requires: []
compatibility:
  languages:
    java: null
selection:
  auto: true
  capabilities: [architecture, coding, review, testing, security]
```

Framework skills remain separate and depend on their language foundation:

```yaml
version: 1
capabilities: [architecture, coding, review, testing, security]
requires: [java-engineering]
compatibility:
  frameworks:
    spring-boot: null
selection:
  auto: true
  capabilities: [architecture, coding, review, testing, security]
```

This preserves the architecture rule:

```text
language != framework != platform != library
```

A future `aws-sdk-java-v2` or `jsign` skill can compose with `java-engineering` and `spring-boot` without being embedded inside either language or framework skill.

## Why the initial compatibility is presence-based

These are broad engineering foundations, not version-specific migration guides. A generic `spring-boot` skill should still apply to a Spring Boot 2 repository as well as a Spring Boot 3 repository, while respecting the detected/pinned version boundary.

More specialized skills can add version constraints such as:

```yaml
compatibility:
  languages:
    java: ">=17,<22"
  frameworks:
    spring-boot: ">=3,<4"
```

The #59 resolver then enforces those constraints conservatively.

## Behavioral eval skeletons

Every shipped Tier-1 skill includes at least one deterministic behavioral eval under:

```text
.agents/skills/<skill>/evals/
```

The initial scenario verifies the foundational behavior expected from every technology skill:

- preserve the detected repository version boundary;
- verify affected behavior with tests;
- do not perform an unapproved technology upgrade.

The mock baseline/candidate provides deterministic CI coverage of the eval contract. These skeletons are expected to grow into richer language/framework-specific behavior cases over time.

## Deterministic pack validation

`sdai.language_packs` validates the Tier-1 assets as a unit. It checks:

- extension envelope is `kind: Pack`;
- `spec.type` is `language`;
- languages/core/framework groups have strict shape and no duplicates;
- every referenced skill exists and has valid #59 resolver metadata;
- each core skill declares compatibility with the pack language;
- each framework skill depends on a core skill from the same pack;
- every pack skill has at least one valid behavioral eval;
- all six required Tier-1 pack IDs are present.

Stable validation errors use `SDAI-LANGPACK-*` codes.

## Existing semantic roles

The same language/framework skills are resolved for the existing semantic roles according to lifecycle capability:

```text
architect          → architecture
 developer          → coding
code-reviewer       → review
tester              → testing
security-reviewer   → security
```

For example, a Spring Boot repository can resolve:

```text
java-engineering
spring-boot
```

for each of those semantic responsibilities without changing role identity or provider selection.

## Technology examples

The packs rely on deterministic #58 technology evidence:

- Java / Spring Boot: `pom.xml`, Gradle
- C# / ASP.NET Core: `.csproj`
- Python / FastAPI / Django: `pyproject.toml`
- JavaScript / TypeScript / Node.js / React / Angular: `package.json`, `tsconfig.json`
- Go: `go.mod`
- PowerShell: `.ps1` and `#requires -Version`

No provider call participates in pack discovery or compatibility resolution.

## Current boundary

The packs are built-in repository assets and are immediately loadable/evaluable/resolvable through the extension, skill, technology, and resolver APIs already present in SDAI.

This slice does **not** introduce remote `pack install` semantics. The later Pack/Catalog milestone will add signed manifests, trusted publishers, dependency locks, catalogs, offline installation, update status, and provenance without changing the role/skill/technology separation established here.
