# SDAI 1.0 Tier-1 Language and Technology Packs

SDAI 1.0 ships six built-in Tier-1 language packs as composable extension assets. Language and framework expertise is selected independently from semantic role and AI provider identity.

```text
semantic role + lifecycle capability
              +
  deterministic repository technology
              +
 agent / policy / requested skill seeds
              +
   compatible auto-selected skills
              ↓
minimal dependency-ordered skill set
              ↓
 provider-neutral execution context
```

The architecture deliberately does **not** create role variants such as `java-developer`, `dotnet-architect`, `python-tester`, `codex-java-developer`, or `claude-spring-reviewer`. A semantic role remains stable while the resolver attaches the smallest compatible technology expertise for the current repository and capability.

## Shipped Tier-1 inventory

Pack composition manifests live under `.sdai/extensions/packs/`. Skills live under `.agents/skills/` and use resolver metadata in `sdai.yaml`.

| Pack | Language foundation | Shipped framework/runtime skills |
|---|---|---|
| `sdai-java` | `java-engineering` | `spring-boot` |
| `sdai-dotnet` | `csharp-engineering` | `aspnet-core` |
| `sdai-python` | `python-engineering` | `fastapi`, `django` |
| `sdai-typescript-javascript` | `javascript-engineering`, `typescript-engineering` | `nodejs-engineering`, `react-engineering`, `angular-engineering` |
| `sdai-go` | `go-engineering` | — |
| `sdai-powershell` | `powershell-engineering` | — |

The six required Pack IDs are validated by `sdai.language_packs.TIER1_LANGUAGE_PACK_IDS`. Tier-1 validation checks the extension envelope, Pack type, language/core/framework structure, referenced skill existence, auto-selection metadata, compatibility rules, framework-to-core dependencies, and behavioral eval presence. Stable validation failures use `SDAI-LANGPACK-*` codes.

A language Pack manifest is a composition contract, for example:

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

The manifest says which skills form the language Pack. It does not collapse language, framework, platform, or library knowledge into one prompt.

## Technology detection

Use the deterministic technology detector before reasoning about skill selection:

```text
sdai tech detect --path .
sdai tech detect --path . --json
```

The stable machine contract is `sdai.technology-report/v1`.

The detector reports evidence across these categories:

- languages;
- frameworks;
- build tools;
- platforms;
- libraries; and
- testing technologies.

Representative Tier-1 evidence includes:

| Technology | Representative deterministic evidence |
|---|---|
| Java / Spring Boot | Maven `pom.xml`, Gradle metadata |
| C# / .NET / ASP.NET Core | `.csproj` SDK, target framework, package references |
| Python / FastAPI / Django | `pyproject.toml` and declared dependencies |
| JavaScript / TypeScript / Node / React / Angular | `package.json`, `tsconfig.json`, package-manager metadata |
| Go | `go.mod` |
| PowerShell | `.ps1`, including `#requires -Version` where present |

No provider/model call participates in technology detection. Detection evidence and selected versions are deterministic repository facts.

## Minimal explainable skill resolution

Resolve the effective skill set with:

```text
sdai skill resolve \
  --agent developer \
  --capability coding \
  --task "implement service change" \
  --path .

sdai skill resolve \
  --agent code-reviewer \
  --capability review \
  --json \
  --path .
```

The stable JSON contract is `sdai.skill-resolution/v1`.

Resolution combines four sources without inventing a new agent identity:

1. skills declared by the semantic agent;
2. skills required by effective policy;
3. skills explicitly requested with repeatable `--skill NAME`; and
4. compatible skills whose `selection.auto` rules match the role/capability/task/domain and detected technology.

For every installed skill, the report records whether it was selected, its origin(s), its dependency list, and the reasons it matched or was rejected. The resolver then expands dependencies in deterministic order.

### Semantic-role invariant

The same technology skills can serve different semantic responsibilities:

```text
architect          → architecture
 developer          → coding
code-reviewer       → review
tester              → testing
security-reviewer   → security
```

For a Spring Boot repository, `java-engineering` and `spring-boot` can therefore be selected for each compatible responsibility while the semantic role and provider routing remain unchanged.

## Skill metadata and composition

A language foundation can opt into automatic selection through repository technology presence:

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

Framework expertise remains a separate skill and declares its dependency:

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

This preserves the design rule:

```text
language != framework != platform != library
```

A later or organization-provided library skill can depend on a language/framework foundation without changing either one. For example, a team may create Jsign, Bouncy Castle, AWS SDK, or Authenticode-specific skills and compose them with Java or PowerShell expertise. **Those names are specialization examples, not claims that such library-specific skills are built into the current Tier-1 set.**

## Version-aware compatibility

A compatibility value of `null` means that presence of the technology is sufficient. This is appropriate for broad engineering foundations such as `java-engineering` or `spring-boot` when the skill teaches practices that apply across supported detected versions.

A specialized skill may require exact numeric constraints:

```yaml
compatibility:
  languages:
    java: ">=17,<22"
  frameworks:
    spring-boot: ">=3,<4"
```

Resolver v1 supports numeric `>=`, `>`, `<=`, `<`, `==`, and `=` clauses separated by commas. A constrained skill is selected only when SDAI can prove an exact compatible version from deterministic evidence. Unknown or unprovable versions fail compatibility rather than being guessed.

This prevents a skill from silently authorizing an unapproved runtime/framework upgrade.

## Policy, project customization, and explicit expertise

Technology auto-selection is only one input. Effective policy can require skills for a capability, and callers can explicitly request additional installed skills with `--skill`. Those seeds still pass capability, role, dependency, and technology compatibility validation.

Repository skill assets use the canonical `.agents/skills/<name>/SKILL.md` layout (with `sdai.yaml` metadata). Legacy `.sdai/skills/<name>` assets remain loadable for compatibility, but a canonical repository skill with the same name takes precedence. This lets a project move to the stable Agent Skills layout without breaking older repositories.

Enterprise/organization standards should be delivered through the extension/Pack and effective-policy mechanisms rather than by creating provider- or language-specific semantic-agent classes. Pack trust, locking, and certification provide the governance boundary for distributing such reusable expertise.

## Behavioral evaluation

Every shipped Tier-1 skill has deterministic behavioral eval scenarios under:

```text
.agents/skills/<skill>/evals/
```

The Tier-1 test suite executes baseline-versus-candidate evals and requires the technology skill to improve the configured behavior. Current foundational cases protect important invariants such as:

- respect the detected repository version boundary;
- verify affected behavior with tests; and
- do not perform an unapproved technology upgrade.

The skill/agent eval report is a stable automation surface (`sdai.eval-report/v1`). Pack certification is the higher-level provider-neutral quality decision for an exact Pack artifact; see `PACK-CERTIFICATION.md`.

## Pack governance is implemented

The old 0.7 guide treated installation, signing, trust, locks, catalogs, provenance, and certification as future work. Those controls are now implemented by the 0.12 Pack/Catalog foundation. Use the dedicated contracts rather than recreating them in language-specific code:

- `PACK-MANIFEST.md` — canonical Pack identity/content manifest;
- `PACK-INTEGRITY.md` — signatures, integrity verification, and trust inputs;
- `PACK-LOCK.md` — exact dependency/Pack resolution lock;
- `PACK-LIFECYCLE.md` — safe install/update/remove/outdated/info/search lifecycle;
- `PACK-CATALOG-POLICY.md` — catalog discovery and policy/trust composition;
- `PACK-CERTIFICATION.md` — eval evidence and provider-neutral Pack quality certification.

Representative lifecycle commands are:

```text
sdai pack install <publisher/id> --lock FILE --source DIR [--json]
sdai pack update  <publisher/id> --lock FILE --source DIR [--json]
sdai pack remove  <publisher/id> [--json]
sdai pack outdated --lock FILE [--json]
sdai pack info <publisher/id> --catalog FILE [--json]
sdai pack search [QUERY] --catalog FILE [--json]
sdai pack certification --source DIR --suite FILE ... [--json]
```

Install/update materialization is bound to exact lock identity plus canonical manifest/content hashes. Managed Pack bytes live under `.sdai/installed-packs/...`; modified/unmanaged bytes are not silently overwritten or deleted. `--local-link` remains an explicit development provenance mode and does not bypass exact-lock verification.

Catalog discovery and Pack lifecycle are deterministic local contracts. Network retrieval is deliberately outside the deterministic lifecycle surface; callers can supply verified Pack/catalog artifacts through their enterprise distribution mechanism.

## Shipped versus follow-on expertise

**Shipped Tier-1:** the six language Packs and skills listed in the inventory table above.

**Not automatically implied by Tier-1:** additional languages (for example Kotlin, Rust, C/C++, Bash), cloud/platform skills (AWS, Azure, GCP, Kubernetes, Docker, Terraform), and library-specific skills (for example Jsign, Bouncy Castle, Testcontainers, Authenticode-specific guidance). Such skills can use the same extension, resolver, eval, Pack, trust, and certification contracts when they are actually added.

Documentation and prompts must not present roadmap examples as installed capabilities. Use `sdai tech detect`, `sdai skill resolve`, and the repository/Pack inventory to determine what is available in a particular project.

## Practical journeys

### Java + Spring Boot

```text
sdai tech detect --json --path .
sdai skill resolve --agent developer --capability coding --json --path .
```

For a detected Spring Boot Java repository, a compatible resolver result includes the language foundation before the framework specialization:

```text
java-engineering
spring-boot
```

The same pair may be selected for architecture, review, testing, or security when the chosen semantic role supports that capability.

### PowerShell

```text
sdai tech detect --json --path .
sdai skill resolve --agent security-reviewer --capability security --json --path .
```

For a detected PowerShell repository, the built-in Tier-1 specialization is `powershell-engineering`. Authenticode or product-specific signing expertise should be supplied as a separate compatible skill/Pack when present; it is not embedded into the PowerShell foundation.

### Explicit organization/project specialization

If a compatible installed skill named `company-secure-java` is required by policy or explicitly requested:

```text
sdai skill resolve \
  --agent security-reviewer \
  --capability security \
  --skill company-secure-java \
  --json \
  --path .
```

The resolver records its `requested` or `policy` origin and expands declared dependencies. It still refuses incompatible role/capability/technology constraints.

## 1.0 boundary

Tier-1 language expertise is therefore not a parallel agent system. It is a governed composition of:

```text
repository evidence
  → technology facts
  → semantic role/capability
  → policy/requested/auto skill resolution
  → dependency-ordered skill context
  → eval/certification evidence
  → Pack integrity/trust/lifecycle controls
```

All of these mechanisms are provider-neutral. They do not implement the held 0.18/#25 identity-backed enterprise approval capability; no SSO/OIDC/GitHub-enterprise approver identity, signature/timestamp, or distinct-approver authorization is implied by Tier-1 skill/Pack governance.
