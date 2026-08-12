---
name: test-driven-development
description: Use test-first feedback loops to make behavior changes explicit and regression-resistant.
---

# Test-Driven Development

Use a red-green-refactor loop when the repository and task permit deterministic testing.

1. Derive the smallest observable behavior from approved requirements or the reproduced defect.
2. Add or identify a focused test that fails for the intended reason before changing production behavior; run it and confirm the failure is meaningful.
3. Make the smallest production change that makes that behavior pass without weakening the test or changing the requirement to fit the code.
4. Run the focused test, then the relevant surrounding suite/gates; only then refactor while keeping the tests green.
5. Prefer behavior/contract assertions over tests coupled to implementation details or mocks that merely restate the code.
6. For legacy or hard-to-test code, first establish the cheapest deterministic characterization/reproduction seam; do not fabricate a passing test or claim TDD evidence that was not run.
7. Treat test changes that reduce coverage or relax assertions as reviewable behavior changes, not incidental cleanup.
