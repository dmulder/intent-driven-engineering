# True-up: import project history into `.intent`

`intentctl trueup` reconstructs why a project evolved and writes the result as actual
intent data. Agents use the ordinary `.intent/intents`, `.intent/bindings`,
`.intent/objects`, `.intent/events`, and manifest records. There is no separate
results database and no review/promotion gate before history becomes available.

Reconstruction is imperfect. Each imported intent records the original commit IDs,
quoted evidence, explicit versus inferred basis, uncertainty, model provenance,
historical lifecycle and relationships. Verification remains `pending`. These are
useful recorded reasons to investigate, rather than claims that an AI recovered
perfect knowledge of what the original author intended.

## Workflow

```sh
intentctl trueup init                        # freeze the Git commit scope, no model or network
intentctl trueup estimate                    # estimate work before choosing providers
intentctl trueup run --no-ai --network off    # optional local inspection only
intentctl trueup run --provider claude --acknowledge-data-sharing --max-commits 20
intentctl trueup run --provider claude --acknowledge-data-sharing
intentctl explain src/auth.py:42 --json
intentctl history src/auth.py:42 --json
intentctl report
intentctl validate
git add .intent
intentctl validate --staged
git commit -m 'Reconstruct historical project intent'
```

A model-enabled run imports supported results automatically, including useful
results from a partially failed run. A new project gets an unknown-provenance
baseline and historical import records together. An already adopted project keeps
its committed baseline and records unchanged. Import never rewrites Git history,
stages files, or changes source. Commit source edits first: importing requires a
clean source worktree and no pending staged source changes.

`run --dry-run` performs analysis without writing canonical results. `--no-ai`
only inspects commits and optionally collects forge evidence; it does not invent
intent from an inspection. A local-only pass needs **both** `--no-ai` and
`--network off`, including when an earlier configuration enabled forge access.

## Actual records and historical meaning

An imported intent has `kind: retrospective` and a structured `reconstruction`
field. Its history contains the source findings, quotes, paths, timestamps,
inferences, refinements, reversals, supersessions and contested relationships.
Successive historical statements become intent revisions. Reanalysis and later
runs append records instead of replacing prior revisions.

Historical bindings pin the original file bytes and byte ranges from the source
commit, or its parent for a deleted file. Their status is `historical` and their
verification is `pending`. They make context available without silently asserting
that an old decision is still a requirement for today's code. An out-of-range or
unavailable target is omitted with a recorded limitation. Full snapshots are
bounded by `limits.max_diff_bytes`; larger historical files remain a documented
gap rather than receiving fabricated bindings.

`explain` includes these ordinary records under `historical`, alongside its
`applicable` bindings. Historical coordinates refer to the pinned source snapshot;
a path association is not proof that an old line still maps to the queried line.
`history`, `show-intent`, `show-binding`, and `report` expose the same canonical
data. `history_coverage` records incomplete analysis so an absent finding is not
mistaken for evidence that no decision existed. `trueup list`, `show`, `tree`, and
`query` also read canonical records, with `I-…` intent IDs.

Import events are recorded **now**, as `retrospective-import` events with unknown
provenance and explicit `import` transitions. They do not pretend that `.intent`
existed in the original commits, retroactively validate uninstrumented changes,
refresh a committed baseline, or satisfy current source-change coverage. The
validator permits these metadata-only transitions with historical, unverified
bindings; ordinary active-binding enforcement continues to apply.

## Processing cache and recovery

The ignored `.intent/cache/trueup/` directory holds provider configuration,
inspection checkpoints, attempts, forge response caches and an internal analysis
graph. It is processing state, not the result agents depend on. You can delete it
after a run; committed history, evidence and queries survive deletion and cloning.
An interrupted analysis needs its checkpoints to resume; deleting the cache starts
analysis again. `--cache-dir PATH` overrides the processing location (`--state-dir`
is a compatibility alias); it never changes where canonical records are imported.

Scope is frozen in parent-before-child order, including merged side branches.
`trueup extend` appends newly reachable commits without reordering existing work.
`status`, `failures`, and `report` describe inspection, external evidence, model
analysis, linking, and import outcomes separately. `retry` repeats failed stages;
`reanalyze SHA...` creates new attempts and appends changed canonical revisions.

Canonical publication validates the whole proposed tree, retains immutable files,
and publishes the manifest last. An ignored transaction journal in `.intent/drafts`
lets a retry finish an interrupted publication without duplicate intents or
rewriting existing records. Conflicting records or a changed manifest cause an
explicit refusal. A failed import is incomplete even when model analysis finished.

| Exit code | Meaning |
| --- | --- |
| 0 | Requested work, including canonical import when enabled, completed |
| 1 | Invalid arguments, unusable cache or refused precondition |
| 3 | Trial or budget limit; rerun to continue |
| 4 | Failed or incomplete analysis, external evidence, linking or import |
| 130/143 | Interrupted; durable progress is retained |

`verify` checks processing-state integrity. Ordinary `intentctl validate` checks
actual imported data. `verify --repair` removes temporary cache files and rebuilds
the internal graph; it does not delete canonical records.

## Providers, evidence and limits

AI is opt-in. Built-in command templates support `claude`, `codex`, `gemini` and
`opencode`; configuration can supply other executables and argument templates.
CLI versions change, so templates are overridable data. Select ordered fallback
providers with repeated `--provider NAME`, and override executables or models with
`--provider-path NAME=PATH` and `--model NAME=MODEL`.

Use `estimate`, `--max-commits`, `--max-ai-calls`, `--max-input-mb`,
`--max-cost-usd`, `--timeout`, and `--jobs` to bound work. Cost limits depend on
provider-reported usage; missing usage is unknown rather than free. Retry backoff,
repair prompts, context splitting, circuit breakers and output byte caps apply.
Configuration JSON supports `providers`, `limits`, `forge`, and `ai` sections.

GitHub/GitLab evidence is optional: `--network forge` enables it. Git remotes are
auto-detected; `--forge KIND:REPO_URL[,api=URL][,token-env=NAME][,alias]` adds explicit
hosts or migration aliases. `--forge-association auto|always|never`, `--forge-depth`
and `--max-forge-requests` control association and traversal. Credentials are sent
only to the configured API origin, and forge tokens are removed from provider
environments. Redirects and pagination cannot carry credentials to other origins.

Every model-authored reference must name supplied evidence; quotes must occur in
that evidence. Unsupported findings are discarded and gaps are reported. Commit
text, diffs, discussions and model output remain untrusted data. Git reads disable
external diff programs, textconv, hooks and inherited repository overrides.
Canonical `.intent` changes are excluded from source evidence to prevent feeding
retained blobs and generated intent back into the model.

## Privacy and limitations

Redacted commit messages, diffs, nearby source and optional forge discussions are
sent to the selected provider. Redaction is pattern-based and cannot find every
secret; use only repositories whose history you may share. `--network off` disables
forge requests, not remote model calls. No paid models or live forges are needed
for the test suite.

Canonical source objects retain **exact historical file bytes**, including any
secrets already present in those Git blobs, because bindings must refer to truthful
snapshots. Review `.intent` before committing or sharing it. Evidence excerpts and
model prompts are redacted, but that is not a sanitization guarantee for historical
source objects.

Quotes establish that text existed, not that its interpretation is correct.
Incomplete, squashed, rewritten or unavailable history weakens reconstruction.
Agents should inspect the evidence and current code, refine these records as they
learn, and distinguish historical context from current applicability and independent
verification. Existing manually authored intent is never overwritten by an import.
