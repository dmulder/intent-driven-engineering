# Writing intent that agents can use

Adapted from Telos's constraint-authoring guide and its experiments (see [lessons](telos-lessons.md)) to this schema. Intent is read by agents who must decide, from text alone, whether a change conflicts with it. Write for that reader.

## Choose what to record

Record what a diff cannot show. Telos's agents caught obvious violations (`3600 -> 86400`) without help. They missed rules that look reasonable in isolation:

- **Policy values with a reason.** "Login is limited to 10 requests per minute" and why (`100` permits credential stuffing). Put the decision and the rejected alternative in `rationale`.
- **Cross-module invariants.** "Archiving a board must not orphan its tasks" is correct in every file locally. Bind it with `constrained-by` to the code that could break it (the archive path) and with `implements` where it is enforced, and link both intents with `--related`. `context` on either file then surfaces the other.
- **Decisions that look arbitrary.** Anything a future agent would be tempted to "simplify".

Do not record what the compiler, type system, or a linter already enforces.

## Requirements

Each `--requirement` should be specific, checkable, and bounded.

| Prefer | Avoid |
| --- | --- |
| Tokens expire within 3600 seconds | Sessions should be secure |
| Error responses never contain user identifiers or internal paths | Code should be clean |
| Archiving a board reassigns or rejects its open tasks | Use `HashMap<String, Vec<Task>>` (prescribes implementation) |
| JWT secrets are read from the environment | `validate_token` on line 42 returns `Member` (brittle; bindings already track location) |

Negative requirements ("never", "must not") define safety boundaries that are easy to cross by accident. Do not encode locations, line numbers, or symbol names in text: bindings carry the exact bytes, and renames would otherwise leave the text lying.

If two requirements genuinely conflict, record both and a third intent stating the resolution (for example "auth errors are generic; validation errors name the field"), linked with `--related`. Never leave an unacknowledged contradiction.

## Severity

`--severity` states how strongly an intent binds. Omitting it means `must`.

| Severity | Meaning for an agent |
| --- | --- |
| `must` | A conflicting change must not land. Change the requirement first through an explicit `modify`, `supersede`, or `retire`, with the user's authorization |
| `should` | Deviation is allowed when the event rationale names the intent and explains why |
| `prefer` | Advisory. Follow it unless there is a reason, and mention material deviations |

Severity changes how an agent weighs a conflict. It never removes the need to declare a transition for every affected binding. Telos saw 50 to 67 percent false rejections once agents were given constraints, so do not mark preferences `must`.

## Bind precisely

Bind the narrowest region that carries the obligation. A whole-file binding makes every edit to the file require a declaration, and tells readers little. Bytes outside every binding have unknown provenance, which `coverage` and `context` report. That is accurate and acceptable; it is not a gap to fill with vague intents.

## Lifecycle and freshness

- Record intent when the work starts, not after. Newly introduced bytes need intent anyway, and a requirement written before the code is not shaped by it.
- End applicability with `unapply` or `retire`; never delete records. Retired and superseded intent is evidence, not a current constraint, and queries hide it by default.
- Revise an intent when the requirement changes. Existing bindings keep their old revision pin and appear as needing reassessment until someone re-examines the code against the new text.
- Treat `status` attention items as decay signals: bindings awaiting reassessment, intents that govern no code, unresolved bindings, and failed verification. Resolve them or tell the user; do not enforce stale text as if it were current.

## Verification status

`declared` is the author's claim. `pending` names evidence that exists but has not been reviewed. `verified` needs `method` and `evidence` from someone other than the author. Never mark a binding `verified` because your own tests passed.
