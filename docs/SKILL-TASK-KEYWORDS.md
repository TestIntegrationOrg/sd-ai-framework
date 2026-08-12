# Skill Task-Keyword Matching

SDAI skill `selection.task_keywords` are matched case-insensitively on token/phrase boundaries. They are not arbitrary substring searches.

Examples:

| Keyword | Task text | Match |
|---|---|---|
| `bug` | `fix a bug` | yes |
| `bug` | `BUG: request fails` | yes |
| `bug` | `bug-fix validation` | yes |
| `bug` | `debug failing regression` | no |
| `bug` | `buggy behavior` | no |
| `api` | `review the API contract` | yes |
| `api` | `capital allocation` | no |
| `security review` | `perform a security review before release` | yes |
| `security review` | `security reviewer handoff` | no |

Matching uses Unicode casefolding before evaluating boundaries, giving the same result on Windows and Linux for the same text. The original configured keyword is preserved in resolver explainability output.

Word characters are considered part of a token. Whitespace and punctuation may form boundaries. This lets `bug` match the token in `bug-fix` while preventing it from matching inside `debug`.

This rule applies only to automatic task-keyword selection. Agent-declared skills, policy-required skills, explicit requested skills, domain filters, capability filters, role filters, technology compatibility, and dependency expansion keep their existing semantics.
