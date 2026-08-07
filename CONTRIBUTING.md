# Contributing

Thank you for contributing to SD-AI Framework.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Design rules

- Keep the SDD control plane provider-neutral.
- Keep agent capabilities and responsibilities explicit.
- Prefer generated/versioned artifacts over hidden conversational state.
- Keep prompts and skills reviewable and version controlled.
- New provider integrations belong behind the provider adapter/plugin boundary.
- Never require broad agent permissions when narrower permissions work.
- New critical decisions should have tests and, where appropriate, an ADR.
- Do not introduce an integration into orchestration core when an adapter boundary is sufficient.

## Adding a provider

Prefer a Python entry-point plugin in the `sdai.providers` group or a custom command profile before modifying core. A provider must implement the SD-AI `Provider` interface and should expose an availability check.

## Adding a skill

Add `skill.yaml` and `SKILL.md` under `.sdai/skills/<name>/`. Declare the capabilities to which the skill applies and keep instructions focused enough to compose safely with other skills.

## Pull requests

Describe the problem, architecture impact, agent/security impact, validation performed, and compatibility concerns.
