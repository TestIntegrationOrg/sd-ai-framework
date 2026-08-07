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

- Keep the core provider-neutral.
- Keep agent responsibilities narrow and explicit.
- Prefer generated/versioned artifacts over hidden conversational state.
- New critical decisions should have tests and, where appropriate, an ADR.
- Do not introduce an integration into the orchestration core when an adapter boundary is sufficient.

## Pull requests

Describe the problem, architecture impact, validation performed, and any compatibility concerns.
