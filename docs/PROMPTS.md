# Prompts

SD-AI stores reusable prompt templates under `.sdai/prompts/`. The default scaffold includes requirements, architecture, planning, coding, review, testing, security, documentation, and general prompts.

Prompts are files so they are version controlled, reviewable, reusable across providers, and testable independently from provider invocation.

Templates may use `{{feature_id}}`, `{{capability}}`, `{{profile}}`, `{{provider}}`, `{{execution_mode}}`, `{{artifacts}}`, `{{skills}}`, and `{{governance}}`.

Unknown required variables fail rendering rather than being silently dropped.

```bash
sdai prompts list
sdai prompts show architect.md
```
