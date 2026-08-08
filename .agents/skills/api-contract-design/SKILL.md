---
name: api-contract-design
description: Design evolvable REST/event contracts using repository-standard OpenAPI, AsyncAPI, and JSON Schema conventions.
---
# API Contract Design

Treat interfaces as version-controlled architecture contracts.

## HTTP/API design

- Follow the repository's approved OpenAPI version and style conventions.
- Model resource semantics, request/response schemas, validation constraints, errors, authentication/authorization requirements, pagination/filtering, idempotency, and concurrency semantics where applicable.
- Use consistent error contracts and avoid leaking internal exception details.
- Define backward-compatibility and versioning expectations before breaking existing consumers.

## Event/API design

- Follow the repository's approved AsyncAPI/schema conventions.
- Define producer/consumer ownership, channel/topic/queue purpose, message key/correlation identifiers, schema, ordering assumptions, delivery semantics, retries, duplicate/idempotency handling, dead-letter/recovery behavior, and compatibility strategy.
- Treat schema evolution and consumer compatibility as architecture decisions.

## Output

Prefer machine-valid YAML/JSON plus concise rationale. Suggested paths: `contracts/openapi.yaml`, `contracts/asyncapi.yaml`, and `contracts/schemas/*.json` or repository equivalents. Do not include credentials or environment-specific secrets in contracts.
