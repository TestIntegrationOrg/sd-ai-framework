---
name: verification-before-completion
description: Require fresh objective evidence before claiming engineering work is complete or correct.
---

# Verification Before Completion

A completion claim is an evidence claim.

- Define the relevant completion evidence from the task, requirements, quality gates, and changed surface before saying the work is done.
- Run the applicable focused tests plus required build/static/security/contract/quality commands; inspect their exit codes and material output rather than assuming command invocation equals success.
- Verify generated/changed artifacts exist in the expected location and that no protected or unrelated artifacts were modified.
- For fixes, reproduce the original failure before/after when practical and retain the regression evidence.
- Distinguish clearly between `passed`, `not run`, `not available`, and `blocked`; never translate an unexecuted check into “should pass.”
- If a required gate is unavailable or failing, report that state and stop short of a completion claim. AI confidence is not a substitute for deterministic gate evidence or human approval.
