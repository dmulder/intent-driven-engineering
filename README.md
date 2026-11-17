# Intent-driven engineering tools

Installable, offline tools for adopting intent-driven engineering in existing Git repositories. Agents maintain explicit reasons for changes so other agents can understand the project and humans can review its decisions. Code, intent, bindings, and logical events evolve independently; Git stores consistent snapshots of all four. The CLI and validator use Python's standard library and Git plumbing. No model, server, runtime Python dependencies, or inline source annotations are required; the optional [true-up](docs/trueup.md) feature can use AI command-line tools and forge APIs only when an operator explicitly asks it to. Install once and run `intentctl` from any supported Git repository, including a nested directory.

**Prototype, not semantic verification.** A successful check proves structural accounting, not that an implementation satisfies its declared intent. See [limitations](docs/git-integration.md) before adopting it.

## Try the complete demonstration

Requires Python 3.10+ and Git on Linux. From this repository:

```sh
python3 -m unittest discover -s tests -v
python3 examples/demo.py /tmp/intent-demo
cd /tmp/intent-demo
python3 tools/intentctl.py explain auth.py:2 --json
python3 tools/intentctl.py history auth.py:2 --json
```

The demonstration makes real commits, overlaps two independent intents on one condition, refactors the implementation, revises intent without changing source, updates the implementation, rejects an incomplete commit, then accepts the completed declarations. [Commands and expected output](docs/demonstration.md).

## Install once for your user

Requires Python 3.10+ and Git; tested on Linux. From a checkout of this project:

```sh
make install                       # default: ~/.local/bin/intentctl
export PATH="$HOME/.local/bin:$PATH"
# Optional custom location:
make install PREFIX="$HOME/opt/intent"
export PATH="$HOME/opt/intent/bin:$PATH"
# Or, without make:
python3 scripts/install.py --prefix "$HOME/.local"
```

The installer copies the engine and protocol into `PREFIX/lib/intent-driven-engineering`
and creates `PREFIX/bin/intentctl`. It needs no root access or network connection and
continues working after the source checkout is moved. Reinstall to upgrade. Existing
unmanaged commands are protected. Run `intentctl uninstall-hooks` in adopted repos
before `make uninstall`; uninstall leaves repository history intact.

## Adopt an existing repository

```sh
cd /path/to/existing/repository
intentctl audit --json             # read-only inventory, also works before adoption
intentctl init --no-hooks          # begin adoption without blocking human commits
# Review generated instructions, then include them in the one-time legacy import:
git add AGENTS.md INTENT.md
intentctl baseline                 # allowed only before the baseline is committed
intentctl validate
git add .intent
intentctl validate --staged
git commit -m 'Adopt intent tooling with unknown legacy provenance'
intentctl report                   # human-readable reasons and adoption backlog
intentctl reconcile --json         # give this review task to a coding agent
```

Initialization records the **index**, including any already staged changes; review
`git diff --cached` first. It never stages files or invents historical intent. Untracked
source is listed by audit but must be deliberately staged to enter governance. Review
sensitive files before staging. Empty/new repositories are supported after `git init`.
The first transaction is an unknown-provenance import only. Introduce explicit intents
in later events, gradually, using the workflow below. Existing tests and behavior are
evidence; their original motivation may remain unknown.

`init` deploys a managed reference in `AGENTS.md` and the agent protocol in `INTENT.md`,
preserving unrelated agent instructions. It refuses conflicting existing `INTENT.md`
files before creating metadata or hooks. Use `init --no-instructions` to manage the
integration yourself, or `deploy` to add instructions separately. `deploy --update`
replaces only an intentctl-managed protocol; review and account for that change.
Nothing copies this project's canonical intents or source code into your repository.

Plain `init` also installs strict hooks; use it after gradual adoption when you want
to require accounting before commits and pushes. Existing hooks and `core.hooksPath`
are preserved. Repeating init preserves metadata. Hooks invoke the engine at its
installed location; rerun init to refresh older managed dispatchers when upgrading
from copied tools or changing the installation location. Original hook backups remain intact.
`uninstall-hooks` restores originals. [Hook details](docs/git-integration.md).

## Monitor human and agent changes

```sh
intentctl audit                    # check committed history plus local changes
intentctl audit --since BASE --json # commits after a known ancestor + local state
intentctl watch --interval 5 --json # foreground polling; emits changes, Ctrl-C stops
intentctl watch --once --json       # one poll, useful for integrations
intentctl reconcile --json          # read-only review context for an agent
intentctl report > /tmp/intent-report.md # Markdown for manual review outside the repo
```

`audit`, `watch --once`, and `reconcile` exit 1 when attention is needed, 0 otherwise.
Unknown legacy coverage is reported as an adoption backlog and alone does not fail
an audit. Untracked files, local changes, invalid committed transitions, unresolved
bindings, and outdated intent assessments need attention. Default audits scan all
reachable history; `--since` explicitly narrows that scope. Shallow clones must fetch
full history. Historical schema-pin drift is reported separately from structural
failures; current snapshots still require the running validator's published schemas.
`watch` caches prior commit results and unchanged snapshots in memory, retries
transient errors, and waits the interval after each completed scan. Its initial scan
can be expensive. It is a foreground process, not a daemon or model invocation.

Human edits may omit intent updates or bypass hooks. Monitoring detects structural
gaps without approving explanations. Agents review diffs, tests, and human context,
record explicit intent changes and uncertainty, and expose their rationale for human
review. A later record cannot make an invalid historical commit valid: committed
drift is identified by commit for branch repair under the repository's review policy.
Never reset a committed baseline to conceal it. See [adoption and reconciliation](docs/adoption.md).

## Record independent intent

The following creates a metadata-only intent and binds it to an existing file. Commands return JSON objects containing generated IDs; substitute the returned IDs for `I-ID` and `B-ID` below. IDs are full SHA-256 digests prefixed by their object type, not the short illustrative identifiers in the assignment.

```sh
intentctl capture --key valid-tokens \
  --description 'Introduce token validity policy' \
  --rationale 'Authentication requires acceptable credentials' --provenance unknown
intentctl intent-create --event valid-tokens --key valid-tokens \
  --title 'Token validity' --description 'Reject invalid tokens' \
  --rationale 'Invalid credentials must not grant access' \
  --requirement 'Accept only valid tokens'
intentctl bind --event valid-tokens --key token-condition \
  --intent I-ID --path auth.py --start 34 --end 45 \
  --rationale 'This expression checks token validity'
intentctl declare --event valid-tokens --intent I-ID \
  --binding B-ID --action introduce
intentctl finalize valid-tokens
git add .intent
intentctl validate --staged
git commit -m 'Introduce token validity intent'
```

Ranges are half-open UTF-8 **byte offsets**, not character positions. Omitting `--start` and `--end` binds the whole file. Bind another intent to the same bytes to represent overlap. Bind the same intent to another region using another binding key.

## Modify governed code

```sh
# Make and stage the intended source edit first.
git add auth.py
intentctl capture --key refactor \
  --description 'Extract authentication helper' \
  --rationale 'Centralize eligibility checks' --provenance unknown
intentctl rebind B-ID --event refactor --path auth.py \
  --start 34 --end 75 --rationale 'Policy is implemented in the helper'
intentctl declare --event refactor --intent I-ID \
  --binding B-ID --action preserve
# Repeat rebind + declare for EVERY affected binding, including overlapping ones.
intentctl finalize refactor
intentctl validate
git add .intent
intentctl validate --staged
git commit -m 'Extract authentication helper'
```

Capture generates `.intent/drafts/E-….json` and a list of required declarations. It **never declares preservation**. Edit draft JSON for verification evidence or advanced many-region records; helpers cover common intent and binding operations. `declare` replaces an earlier declaration for the same intent/binding pair, so run it after preparing the corresponding revisions.

`--provenance unknown` acknowledges unbound *legacy before-bytes*; it does not waive known-intent accounting and cannot introduce unbound after-bytes. Newly introduced bytes always require at least one binding (`NEW_CODE_REQUIRES_INTENT` otherwise). `known` requires bindings to cover changed bytes in both existing source states. Leaving provenance unset prevents finalization.

Bindings that do not overlap a changed interval are not required to rebind. Their pins may remain on older snapshots when deterministic equal-region mapping uniquely preserves their bytes (coordinate maintenance, not semantic inference). Ambiguous moves still need an explicit `rebind`. Use `--verification-status` on bind/rebind to separate claimed preservation from independently supported verification.

For an intent-only change, capture with no staged source edits, use `intent-revise I-ID --event KEY --requirement TEXT`, and declare `modify`. Old bindings remain pinned to their exact revision and appear in `status`/`impact` as needing reassessment. Use `rebind --intent-revision N` in a later event to acknowledge the new policy.

## Query and diagnose

```sh
intentctl status --json
intentctl explain auth.py:2 --json
intentctl explain auth.py:2 --at HEAD~1 --json
intentctl history auth.py:2 --json
intentctl show-intent I-ID --revision 1 --json
intentctl show-binding B-ID --json
intentctl impact I-ID --json
intentctl doctor --json
intentctl validate --commit HEAD
intentctl validate --range BASE..HEAD
```

Human output is indented JSON for records, and concise diagnostics for validation. JSON validation returns `valid`, `errors`, stable codes, paths, and messages; failure exits nonzero. `doctor` diagnoses without modifying history. Repair drafts or append corrective records; never overwrite committed revisions.

## Retrofit an existing project: true-up

A repository that never recorded intent can still be queried. `intentctl trueup` reads Git history (and, if you allow it, GitHub/GitLab pull requests, issues and reviews), optionally asks an AI CLI (`claude`, `codex`, `gemini`, `opencode`, or your own command) to extract reasons, decisions, constraints and rejected alternatives, and keeps the result in a **separate, non-authoritative store** under `.git/intent-trueup/`. Discovery does not touch the working tree, the index, history, or `.intent/`. Explicit reviewed `trueup promote` creates canonical metadata in `.intent/`.

```sh
intentctl trueup init
intentctl trueup estimate
intentctl trueup run --no-ai --network off                      # local inspection only, no model, no network
intentctl trueup run --provider claude --acknowledge-data-sharing --max-commits 20 --network forge
intentctl trueup status                           # coverage; a run with failures is never "complete"
intentctl trueup retry --provider claude --acknowledge-data-sharing --network forge
intentctl trueup query src/auth.py:42             # also shown by `explain`/`history` as a separate section
```

Runs are resumable and isolate failures per commit. Discoveries are inferred, labelled as such, and become canonical intent only through an explicit `trueup review` and `trueup promote`, which reuse the normal capture/finalize/validate flow and record verification as `pending`. Read [docs/trueup.md](docs/trueup.md) for privacy, cost, configuration and limitations before pointing it at a model.

## Design and status

- [Architecture and tradeoffs](docs/architecture.md)
- [Data model and canonicalization](docs/data-model.md)
- [Validator specification](docs/validator.md)
- [Git support and limitations](docs/git-integration.md)
- [Security and trust](docs/security.md)
- [True-up: retrospective intent discovery](docs/trueup.md)
- [Reproducible demonstration](docs/demonstration.md)
- [Roadmap](docs/roadmap.md)
- [Agent entry point](AGENTS.md) and [mandatory intent protocol](INTENT.md)

Canonical history is versioned JSON. Drafts and future caches are disposable. Source snapshots intentionally trade storage efficiency for durable intermediate states independent of commit boundaries. V1 scans metadata and uses explicit lineage; it has no AST matching, automatic identity inference, or persistent code-fragment IDs.

Licensed under [MPL-2.0](LICENSE).
