# Skills

Skills are reusable, provider-neutral instruction packages attached to agent profiles.

```text
.sdai/skills/architecture-review/
├── skill.yaml
└── SKILL.md
```

Example manifest:

```yaml
name: architecture-review
description: Evaluate architecture options and ADRs.
capabilities: [architecture, review]
```

Starter skills: `spec-traceability`, `architecture-review`, `secure-coding`, and `test-design`.

A skill listed on a profile is injected only when its manifest supports the current capability.

```bash
sdai skills list
sdai skills show architecture-review
```
