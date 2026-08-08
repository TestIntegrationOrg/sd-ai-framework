---
name: plantuml-sequence
description: Create self-contained PlantUML sequence diagrams for normal, failure, retry, timeout, and asynchronous flows.
---
# PlantUML Sequence Diagrams

Generate valid, self-contained `.puml` sequence-diagram source.

## Rules

- Start with `@startuml` and end with `@enduml`.
- Use architecture-level participants (actor, service, datastore, queue/topic, external dependency) rather than method-by-method implementation noise.
- Keep participant names consistent with C4 and architecture documents.
- Label messages with business/technical intent and relevant protocol or event name when useful.
- Use `alt` for mutually exclusive outcomes, `opt` for optional behavior, `loop` for bounded retries/polling, and `par` for meaningful concurrency.
- Show important timeout, retry/backoff, idempotency, duplicate-delivery, failure, compensation/rollback, and recovery paths when relevant.
- Distinguish request/response behavior from asynchronous/event-driven behavior in labels and notes.
- Show trust/authentication transitions when they materially affect the flow.
- Avoid remote includes unless the repository explicitly permits them.

Suggested filename: `architecture/diagrams/<scenario>-sequence.puml`.
