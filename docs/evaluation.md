# Evaluating whether intent helps agents

No experiment in this repository measures agent behavior. The tests prove the tool computes and enforces what it documents; they do not prove agents decide better with it. This protocol records how to find out without repeating mistakes Telos documented in its own reviews (see [lessons](telos-lessons.md)).

## Hypotheses

1. With intent surfaced, an agent identifies conflicts that its diff does not reveal more often than with Git history alone.
2. With intent surfaced, an agent does not reject benign changes more often than with Git history alone.
3. The structured, enforced form adds measurable value over a flat file of the same rules. Telos saw no difference between its store and a flat `CONSTRAINTS.md` at N=5.

## Conditions

Run every scenario under all of these with an identical prompt template where only the context block differs:

| Condition | Context |
| --- | --- |
| `git-only` | Commit message and diff |
| `flat-file` | The same rules as prose in a single markdown file |
| `intent-context` | `intentctl context PATH --json` for the changed files |
| `intent-protocol` | The full `AGENTS.md` loop, including capture and declarations |

The prompt must offer approval and rejection symmetrically ("approve if compliant"). Telos's first prompt primed agents to find problems.

## Scenarios

Mix scenario types, and include at least as many benign changes as violating ones.

- **Policy value**: a configuration default contradicts a recorded decision.
- **Cross-module invariant**: each file is locally correct; the violation spans two.
- **Interaction**: two individually reasonable optimizations jointly break a guarantee.
- **Obvious violation** (control): expected to be caught by every condition. If it separates conditions, the scenario is mis-scored.
- **Benign change** (false-positive control): bugfix, rename, internal cache. Expected to be approved.
- **Stale intent**: the code evolved and the recorded text no longer applies. Expected: the agent flags reassessment rather than enforcing it.

`tests/test_agent_queries.py` already builds the first two plus the benign-edit control as deterministic fixtures. They verify that the right text is retrievable per file, not what an agent does with it.

## Measures

Score each trial separately and blind the judge to the condition.

- Caught the specific violation (not merely "rejected"; Telos saw agents reject for unrelated reasons).
- Cited the governing intent ID or requirement text.
- Falsely rejected a benign change.
- Treated a `prefer` or `should` intent as binding when it was not.
- Correct handling of stale intent.
- Tokens and wall time per condition.

## Statistics

Report counts with confidence intervals. Use at least 10 trials per cell, preferably 30, across more than one model family. Telos's N=2 and N=5 results were explicitly directional. Keep raw per-trial records in the repository, and do not publish a pass/fail headline.

## What would change our design

- If `flat-file` matches `intent-context` and `intent-protocol` on every measure, retrieval is not our value; the claim reduces to enforcement and lifecycle and should be argued on those terms.
- If false rejections rise with intent, strengthen severity guidance and consider a stricter default for new intents.
- If agents ignore `context` unless forced, the protocol should invoke it from `capture` or the hooks rather than rely on instruction text.
