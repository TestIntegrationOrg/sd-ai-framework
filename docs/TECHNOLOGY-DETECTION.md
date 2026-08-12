# Repository Technology Detection

SDAI 0.7 introduces a deterministic, provider-independent repository technology model. The detector exists so semantic agents remain role-based (`developer`, `architect`, `tester`, `security-reviewer`) while the later skill resolver can attach only the language, framework, platform, library, and testing skills relevant to the current repository/task.

## Command

```bash
sdai tech detect
sdai tech detect --json
sdai tech detect --path /path/to/project
```

The command requires an initialized SDAI workspace. Detection never calls an AI provider.

## Categories

The stable v1 JSON model groups facts into:

- `languages`
- `frameworks`
- `build_tools`
- `platforms`
- `libraries`
- `testing`

Each technology fact records:

- canonical technology ID
- selected version/constraint, if deterministically known
- `version_source`: `detected`, `declared`, `ambiguous`, or `none`
- all distinct detected versions
- whether the repository explicitly declared the technology
- every repository-relative evidence path and detector signal

Repository paths are emitted in POSIX form on Windows and Linux.

## Evidence currently recognized

| Evidence | Examples |
|---|---|
| `pom.xml` | Java, Maven, Spring Boot, Quarkus, AWS SDK Java v2, Jsign, MongoDB, JUnit |
| `build.gradle`, `build.gradle.kts` | Gradle, Java/Kotlin, Spring Boot, Quarkus, AWS SDK, JUnit |
| `*.csproj` | C#, .NET, ASP.NET Core, AWS/Azure NuGet signals, MongoDB, xUnit/NUnit/MSTest |
| `pyproject.toml` | Python constraint, FastAPI, Django, boto3/AWS, pytest |
| `package.json`, `tsconfig.json` | JavaScript/TypeScript, Node.js, package manager, React, Angular, Express, Next.js, NestJS, AWS/Azure/GCP, Vitest/Jest/Mocha |
| `go.mod`, `go.work` | Go version/build tooling and AWS SDK signal |
| `Cargo.toml` | Rust version and Cargo |
| `*.ps1` | PowerShell and `#requires -Version`; Pester signal |
| `Dockerfile*` | Docker |
| `*.tf` | Terraform |

Detection is intentionally conservative. A file may prove a technology exists without proving its version.

## Language version is not runtime/framework version

SDAI does not infer one technology's version from another technology's version.

For example:

```xml
<PropertyGroup>
  <TargetFramework>net8.0</TargetFramework>
  <LangVersion>12.0</LangVersion>
</PropertyGroup>
```

produces approximately:

```text
languages.csharp = 12.0
frameworks.dotnet = 8.0
```

If `LangVersion` is absent, C# remains detected with no language version. SDAI does **not** report C# 8 merely because the project targets .NET 8.

The same principle applies throughout the detector: Java, Spring Boot, Maven, AWS SDK, Jsign, Python, FastAPI, Node.js, TypeScript, and other technologies keep independent identities and version evidence.

## Explicit technology declarations and pins

A repository may add:

```yaml
# .sdai/technology.yaml
version: 1

languages:
  java: "17"
  powershell: null

frameworks:
  spring-boot: "3.4.10"

build_tools:
  maven: null

platforms:
  aws: null

libraries:
  aws-sdk-java-v2: "2.29.27"
  jsign: "7.4"

testing:
  junit: null
```

A non-null value is an explicit version/constraint pin and becomes `version_source: declared`.

A null value means **the repository explicitly declares the technology but does not pin its version**. If one version is independently detected, that detected version remains usable. This allows organizations to confirm required technologies without erasing repository evidence.

Explicit configuration is fail-closed:

- unknown top-level fields are rejected
- only schema `version: 1` is accepted
- category values must be mappings
- technology IDs use a restricted portable identifier grammar
- version values must be string/number/null

## Conflicting or ambiguous versions

When different repository evidence reports multiple versions for the same technology and there is no explicit pin, SDAI returns:

```text
version = null
version_source = ambiguous
```

with finding `SDAI-TECH-005`. It does not guess which version is authoritative.

An explicit pin resolves the selected version while retaining all detected versions as evidence. If the explicit version is not among the detected versions, SDAI emits `SDAI-TECH-004` so the override remains explainable rather than silently masking drift.

## Scan boundaries

The detector recursively scans the repository but skips common generated/dependency/cache directories including `.git`, `.gradle`, virtual environments, `node_modules`, `target`, `build`, `dist`, coverage/cache directories, and `__pycache__`.

Directory and file symlinks are not followed. Archived SDAI specification history under `specs/archive` is also excluded so historical promotion evidence cannot influence the repository's current technology model.

Malformed optional evidence such as an invalid `package.json`, `pom.xml`, `.csproj`, `pyproject.toml`, or `Cargo.toml` produces a structured warning instead of inventing a result. Invalid explicit `.sdai/technology.yaml` configuration is a hard error because it is an intentional repository contract.

## Finding codes

| Code | Meaning |
|---|---|
| `SDAI-TECH-001` | explicit `.sdai/technology.yaml` is invalid |
| `SDAI-TECH-002` | repository evidence could not be read safely as UTF-8 |
| `SDAI-TECH-003` | optional technology evidence is malformed |
| `SDAI-TECH-004` | explicit version pin conflicts with detected version evidence |
| `SDAI-TECH-005` | multiple detected versions are ambiguous without a pin |

## Trust boundary

```text
repository files + explicit technology config
                    ↓
        deterministic SDAI detectors
                    ↓
       versioned technology report
                    ↓
          future Skill Resolver
                    ↓
       minimal role-specific skills
                    ↓
            Provider Router
```

The detector does not create `java-developer`, `dotnet-architect`, or provider-specific agents. Technology is context for semantic roles, not part of role identity.
