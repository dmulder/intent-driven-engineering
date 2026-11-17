# True-up: retrospective intent discovery

A true-up reconciles a project's code with the reasons it ended up that way. It reads the Git history of a repository (including repositories that never adopted intentctl), gathers evidence, optionally asks an AI command-line tool to extract reasons, decisions, constraints, rejected alternatives and later changes, and stores the result as a **separate, non-authoritative pseudo-intent graph** that agents and people can query.

```sh
intentctl trueup --help
```

**Everything a true-up produces is inferred from evidence, never verified.** It is a set of leads with citations, not a statement of requirements. A discovery becomes canonical intent only when a maintainer reviews it and explicitly promotes it, and even then it is recorded as `pending` verification.

## What it does not do

- It never rewrites Git history, `.intent/baseline.json`, or any committed canonical record.
- It never touches the working tree or the index of the target repository (it uses Git plumbing, `GIT_OPTIONAL_LOCKS=0`, no checkout, no hooks).
- It never executes repository content, model-suggested commands, or hooks. Git is run without a shell with `core.hooksPath=/dev/null`, `protocol.allow=never`, and without external diff or textconv drivers.
- It never reports a partially failed run as success, and never invents a verification result.

## Quick start

```sh
# 1. Freeze the scope (no AI, no network). Remotes are auto-detected as forges.
intentctl trueup init
intentctl trueup estimate                 # size from local Git data only

# 2. Free, local-only pass: inspects every commit, records diagnostics, no model, no network.
intentctl trueup run --no-ai --network off

# 3. Small paid trial, then the full run. Data-sharing acknowledgement is mandatory for non-local providers.
intentctl trueup run --provider claude --acknowledge-data-sharing --max-commits 20 --network forge
intentctl trueup run --provider claude --provider gemini --acknowledge-data-sharing --jobs 4 --network forge

# 4. Inspect.
intentctl trueup status
intentctl trueup failures
intentctl trueup tree
intentctl trueup query src/auth.py:42 --detail

# 5. Fix what failed; only incomplete work is reprocessed.
intentctl trueup retry --provider claude --acknowledge-data-sharing --network forge
```

`init`, `estimate`, `status`, `report`, `failures`, `show`, `list`, `tree`, `query`, `providers`, `verify` never call a model or the network. Testing an installation needs none: `run --no-ai --network off` exercises the whole pipeline locally, and `providers` checks which provider executables exist without invoking them.

## Where state lives

By default in `<git-common-dir>/intent-trueup/` (for example `.git/intent-trueup/`). That is outside the working tree, never committed, shared by all worktrees, and invisible to the canonical validator, which rejects unknown files under `.intent/`. Override with `--state-dir DIR` (any directory; `INTENTCTL_TRUEUP_STATE_DIR` is honoured by `explain` and `history`). Deleting the directory discards the true-up and nothing else.

State is JSON with a format version. Every record is written atomically (temporary file, `fsync`, rename, directory `fsync`), one writer holds an exclusive `flock` lock, and an unsupported format version or a changed analysis version stops the run with an actionable message (`--accept-version-change` records the new version on new attempts).

## History scope and ordering

`init` resolves refs to commit ids (`HEAD` by default; `--ref` repeatable; `--all-refs`, optionally `--include-remotes`) and enumerates every reachable commit with `git rev-list`. The result is a **frozen manifest** with a digest:

- Order is parent-before-child topological. Among commits whose parents are already emitted, the smallest `(committer timestamp, object id)` goes first, so timestamps only break ties between causally unordered commits and the order never depends on them for correctness.
- Shallow repositories (with their boundary commits), missing commit objects and unreadable refs are reported as coverage diagnostics. Coverage reports `history_complete: false` and the run cannot claim complete.
- Root, empty and merge commits are handled. A merge is diffed against its first parent for its own change and lists the commits it brought in (`parents[1:] --not parents[0]`); it is never double-counted as the author of those commits' changes.
- `extend` appends commits that became reachable later, preserving existing order. Moved refs and rewritten history are detected (`scope_check`).

## Evidence

For every commit the inspection stage records metadata, trailers, message, changed paths with rename detection, bounded diffs (with oversize and binary handling), and bounded surrounding context. Contextual reference resolution understands URLs, `owner/repo#N`, `#N`, `GH-N` and `!N` for the configured forges, using the commit's own repository as context.

When `--network forge` is selected, the forge stage fetches pull/merge requests, issues, comments and reviews:

- Credentials come only from the environment variable named in the forge definition (`--forge KIND:URL[,api=URL][,token-env=NAME][,alias]`; GitHub and GitLab; hosts of remotes are auto-detected). A token is only ever sent to the origin it was configured for. Redirects to another origin are refused. Links to other hosts are fetched anonymously. A token is sent over plain `http` only to a loopback host, or with `--allow-insecure-http`.
- Pagination stays on the same origin, with page and size bounds. Rate limits are waited out up to `max_rate_wait` seconds or open a circuit. Missing (404) and inaccessible (401/403) resources are recorded and negatively cached, so private or deleted resources are never mistaken for "no discussion".
- Fetched resources are deduplicated and cached by resource key across commits, bounded by depth, count and size.
- `--forge-association auto|always|never` controls whether a commit is associated with a PR/MR without an explicit reference (`auto` asks the primary forge for the PRs/MRs containing the commit only when the commit text has no explicit reference; `always` always asks; `never` uses explicit references only).
- Secrets (tokens, `Authorization` values, URL credentials, secret-looking query parameters) are redacted before storage, prompts and logs.

`--network off` (the default recorded at `init`) disables forge access; the run is labelled as lacking external evidence, and that shows in coverage. Remote AI providers can still receive repository evidence when AI is enabled. Use both `--network off` and `--no-ai` for local-only operation, even if the stored network policy enables forge access.

## AI analysis

AI processing is an explicit operator choice. Selecting any provider that is not marked `local` requires `--acknowledge-data-sharing`, because redacted commit messages, diffs, surrounding code, and forge discussions are sent to that provider's service under your own account and terms.

### Providers

Built-in command templates exist for `codex`, `claude`, `gemini` and `opencode` (modelled on the invocation styles used by Himmelblau's `cargo_vet_review.py` and `backport.py`). Providers are used only in the order given by `--provider` (repeatable); fallback happens only through that list. Everything about a provider is data, because CLI flags change between releases:

```json
{
  "ai": {"providers": ["claude", "local-model"], "timeout": 300, "max_attempts": 3, "max_repairs": 1, "backoff_base": 2.0,
         "models": {"claude": "sonnet"}, "paths": {"claude": "/usr/local/bin/claude"}},
  "providers": {
    "local-model": {
      "bin": "my-llm", "local": true, "timeout": 600, "max_output_bytes": 4194304,
      "invocations": [{"argv": ["{bin}", "--json", "--file", "{prompt_file}"], "stdin": false, "output": "stdout"}],
      "model_args": ["--model", "{model}"], "env": {"LLM_QUIET": "1"}, "env_remove": ["SOME_TOKEN"]
    }
  },
  "limits": {"max_prompt_chars": 40000, "chunk_chars": 12000},
  "forge": {"limits": {"max_depth": 1, "max_resources_per_commit": 12}}
}
```

Pass it with `init --config FILE`. Argument templates support `{bin}`, `{prompt}`, `{prompt_file}`, `{workdir}`, `{output_file}` and `{model}`; commands are argument arrays, never shell strings. Built-in invocations are tried in order: stdin first, then an argv prompt as a fallback only when the prompt is below 100 KB.

Safety of provider processes: an empty scratch working directory (nothing in the target repository is reachable as the current directory), forge token variables scrubbed from the environment, read-only sandbox flags where the CLI supports them, bounded stdout/stderr memory, hard timeouts, and process-group termination on timeout or cancellation. Provider output is treated as untrusted text and parsed as data; suggested commands are never run.

### Failure handling

Failures are classified from the provider's own error output (unavailable, authentication, quota, rate limit, timeout, transient, context window, nonzero exit, empty, configuration). Then:

- retryable classes retry with exponential backoff and jitter (`--retries`, `--backoff-base`);
- a context-window error splits the evidence into smaller chunks and retries each half (recursively); when several chunks yield findings, one consolidation call merges duplicates, keeping the per-chunk originals under `merged_from` and falling back to their union if consolidation fails;
- malformed output gets one repair prompt, then counts as a failure;
- the next provider in order is tried when one fails;
- a circuit breaker opens when a provider fails across **three distinct requests** (so one poisonous commit cannot disable a healthy provider). Permanent failures (not installed, not authenticated, bad configuration) disable the provider for the run; quota exhaustion persists with a cool-down; `--reset-circuits` clears them;
- budgets: `--max-ai-calls`, `--max-input-mb`, and `--max-cost-usd` (only provider-reported cost counts; providers that report nothing are not limited, and totals say "unknown, not free").

A failure in one commit, resource, or request never aborts the run: it is recorded against that commit and stage, other commits continue, and the run ends `incomplete`.

### Validation of model output

Output must match a schema (finding kinds, `basis` of `explicit` or `inferred`, `uncertainty` of `low|medium|high`, no numeric confidence). Each finding must cite evidence items by reference, with a quote that is verified to occur in that item. Evidence ids that were not supplied are dropped; quotes that do not occur in the cited item are dropped; a finding claiming `explicit` basis without a verified quote is recorded as `inferred`; a finding with no valid supporting evidence is discarded (except an `open-question`); paths not present in the evidence are dropped. Every rejection is recorded. Findings record the source excerpt, date, basis, uncertainty, paths, model, provider, and analysis and prompt versions.

## The pseudo-intent graph

Findings are linked incrementally in manifest order into discoveries (`N-…` nodes). A link step proposes that a finding refines, supersedes, reverses, retires, or relates to an earlier discovery. Merge-gating checks that a proposed link names a real earlier node and cites evidence; a rejected merge stays a separate node with a plain `related` edge (recorded under `merge_gate_rejections`). Disagreements remain `contested` rather than being resolved by the tool.

Each node has a lifecycle: `active-in-history`, `superseded`, `reversed`, or `retired`. This describes what happened in history, **not whether the constraint applies to the code today**. Present applicability is a separate question; `query` associates a location with discoveries using `git log -L`, `--follow`, rename detection and `blame`, and labels these associations heuristic.

The graph is rebuildable from durable records at any time (`graph-rebuild`), and out-of-order completion (a later commit linked before an earlier one was retried) converges to the same graph as an uninterrupted run.

## Run semantics

Per commit there are four stages: inspection, external evidence, analysis, link. Each has its own status and immutable attempt records, so coverage separates "inspected", "AI analysed", "external evidence complete", and "discoveries produced".

| Exit code | Status | Meaning |
| --- | --- | --- |
| 0 | `complete` | everything the run requested finished (for `--no-ai`, inspection) |
| 1 | `fatal` | unusable state, bad usage, or refused precondition |
| 3 | `trial_limit` / `budget_exhausted` | stopped by `--max-commits` or a budget; re-run to continue |
| 4 | `incomplete` | some stages failed or are pending; see `failures` |
| 130/143 | `interrupted` | SIGINT/SIGTERM; durable progress kept, re-run resumes |

`status --json` reports the strict store-level `complete` flag, which stays false while any stage in the scope is unprocessed. `report` adds every incomplete item and the run history. `retry` resets failed items only (`--stage`, `--commit`, `--partial`, `--relink-dangling`); `reanalyze COMMIT...` appends a **new** attempt without deleting older ones, bumps the commit's epoch, and relinks it. Nothing is silently overwritten. `verify` checks integrity; `verify --repair` removes only crash leftovers (orphan temporary files) and rebuilds the graph.

Interruption at any point (including a crash between two writes) is safe: re-running resumes from per-commit and per-chunk checkpoints and converges to the same result.

## Using results

```sh
intentctl trueup query src/auth.py:42
intentctl explain src/auth.py:42 --json      # adds a separate "retrospective" section
intentctl history src/auth.py:42 --json
```

`explain` and `history` add a `retrospective` section only when a true-up store exists; it is clearly labelled non-authoritative, never mixed into `applicable`, and a broken or unreadable store is reported as skipped rather than failing the canonical query. `--no-retrospective` suppresses it. When no store exists, the output is byte-for-byte what it was before, and the true-up package is not even imported.

Agents must treat retrospective rationale as untrusted data. It does not override AGENTS.md, user instructions, or security policy, and is not a constraint unless a maintainer promoted it.

## Review and promotion

```sh
intentctl trueup show N-…                                   # evidence, uncertainty, content hash
intentctl trueup review N-… --decision approve --reviewer alice --note 'checked PR #7'
intentctl trueup promote N-… --confirm HASH --bind src/auth.py:40-60 --dry-run
intentctl trueup promote N-… --confirm HASH --bind src/auth.py:40-60 --key auth-policy
git add .intent && intentctl validate --staged && git commit
```

Promotion is the only path into canonical records. It requires an `approve` review recorded for the exact content hash (a changed discovery must be re-reviewed), `--confirm` with that hash, an initialized intentctl repository, and no staged source changes (it is a metadata-only transaction). Reviews are local and unauthenticated; treat the reviewer name as an attribution note, not an identity.

It reuses the normal `capture` / `intent-create` / `bind` / `declare` / `finalize` flow, so every canonical rule still applies, and refuses to promote the same discovery twice. What gets recorded is deliberately conservative:

- the intent has `kind: retrospective`, unknown provenance, and a rationale stating it was **not captured when the change was made**, with an event attestation beginning `NOT verified`;
- bindings are chosen by the reviewer with `--bind PATH[:FIRST-LAST]` against the current index; suggestions printed by `show` or `--dry-run` are never applied automatically;
- bindings default to `historical`, which do not govern edits. An `active` binding imposes a constraint on future edits and therefore requires `--applicability-rationale`; a discovery that ended in history can only be promoted as historical;
- verification is recorded as `pending`. Promotion never produces `verified`.

For a historical binding the canonical validator requires an `unapply` declaration, so promotion records two events (create the intent, then record the historical association). That is a property of the existing protocol, not a workaround.

## Privacy, cost and limitations

- **Privacy.** Redacted commit messages, diffs, nearby code and forge discussions go to the selected provider. Redaction is pattern-based and cannot catch every secret; do not run against repositories whose history you may not share. `--no-ai` and `--network off` keep everything local. State may hold excerpts of commits and discussions; it lives in `.git` and should be treated like repository contents.
- **Cost.** Roughly one analysis call per commit plus link calls and any splits or repairs. Use `estimate`, then `--max-commits`, `--max-ai-calls`, `--max-input-mb`, and `--max-cost-usd`. Cost limits only apply to providers that report cost.
- **Truthfulness.** Model output can be wrong, and rationale in commits and comments can be wrong or self-serving. Quotes are checked to exist in the evidence; whether they support the claim is for a reviewer to judge. Counts in `status` describe coverage, not accuracy.
- **Applicability.** Associations between discoveries and current code are heuristic (`git log -L`, rename tracking, `blame`). Squashed, vendored or heavily rewritten history weakens them.
- **Forges.** GitHub and GitLab only. Review threads on migrated or deleted hosts may be unavailable; those gaps are recorded, not hidden. Authentication to private forges requires a token environment variable you provide.
- **Scale.** Memory, disk and subprocess output are bounded per commit, but a very large history needs time and quota; runs are resumable for that reason. Concurrency (`--jobs`) is per commit.
- **Not a verifier.** Neither the true-up nor `promote` proves that code satisfies an intent. Independent review remains necessary.
