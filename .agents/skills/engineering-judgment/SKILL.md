---
name: engineering-judgment
description: Apply senior engineering judgment so agents make safe, useful progress without turning every uncertainty into a blocker.
---
# Engineering Judgment

Use this skill across the software-development lifecycle to balance rigor with forward progress.

## Classification contract

Classify material statements and gaps using these categories:

- **Known** — directly supported by approved requirements, repository evidence, contracts, code, tests, or governance.
- **Proposed** — a concrete engineering recommendation that is reasonable from current evidence but is not yet an approved business or architecture decision.
- **Assumption** — a temporary working assumption used to make progress; state impact and how it will be validated.
- **Open question** — a decision that needs an identified owner because available evidence cannot resolve it safely.
- **Blocker** — missing information or unresolved risk that would make the next lifecycle action unsafe, invalid, materially misleading, or likely to require expensive rework.

Do not collapse all uncertainty into `Open question` or `Blocker`.

## Progress rules

1. Preserve explicit business intent and approved decisions. Never silently replace them.
2. When the repository provides enough evidence for a conventional engineering choice, make a **Proposed** recommendation instead of asking a generic question.
3. Use **Assumption** only for reversible decisions with bounded risk. State the validation or revisit trigger.
4. Escalate to **Open question** when the choice depends on business policy, regulatory/compliance interpretation, ownership, budget, externally controlled contracts, user experience intent, or another authority the agent cannot legitimately decide.
5. Use **Blocker** sparingly. A blocker must explain exactly which next action cannot safely proceed and why.
6. Prefer a usable baseline plus a short decision list over a long inventory of theoretical concerns.
7. Distinguish required-now concerns from later hardening or optimization. Avoid speculative enterprise ceremony that is not material to the feature.
8. Keep framework/runtime diagnostics out of feature requirements and architecture artifacts unless they directly affect the feature.
9. For security-sensitive work, remain conservative about trust, authorization, secrets, keys, data exposure, abuse paths, and fail-open behavior, while still proposing standard controls when evidence supports them.
10. For every material proposal, preserve traceability to the requirement, risk, constraint, or evidence that motivated it.

## Decision test

Before escalating a question, ask:

- Can a senior engineer make a safe, reversible default from repository evidence and accepted engineering practice?
- Would the choice change business behavior, trust policy, compliance posture, externally visible compatibility, or a costly-to-reverse architecture boundary?
- Would proceeding without an answer create security exposure, data loss, contract breakage, or major rework?

If the first answer is yes and the latter two are no, prefer **Proposed** or **Assumption** over blocking.

## Output discipline

A lifecycle review should make the next engineer more effective. Prefer:

1. concise disposition/status;
2. known baseline;
3. proposed decisions or requirements;
4. assumptions with validation;
5. only the material open questions;
6. explicit blockers, if any;
7. recommended next action.

Do not use governance language as a substitute for engineering analysis.
