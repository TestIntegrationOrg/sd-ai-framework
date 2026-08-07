---
name: test-design
description: Build risk-based automated tests tied to acceptance criteria and critical failure modes.
---
# Test Design

- Trace tests to acceptance criteria and material NFRs.
- Cover success, boundary, failure, authorization, retry/idempotency, and recovery behavior where relevant.
- Use deterministic test data and isolate external dependencies appropriately.
- Include contract/integration/resilience/security tests when unit tests cannot prove the behavior.
- Flag acceptance criteria that are not observable or objectively testable.
