# Adoption, intent maintenance, and human review

Install `intentctl` once with `make install`. The installed engine discovers the
current Git root at runtime; each repository owns its independent `.intent/`
records. No central server, provider credentials, or embedded AI client is required.
The current snapshot engine supports ordinary Git files, binaries, symlinks, modes,
and linked worktrees. Submodules, non-UTF-8 filenames, and merge reconciliation
remain unsupported and produce diagnostics. Thus “any repository” is the product
goal, with these prototype limits still applying.

## Gradual adoption

Start with `init --no-hooks`. Review the index, stage generated `AGENTS.md` and
`INTENT.md`, refresh the uncommitted `baseline`, stage `.intent`, validate, and
commit the import. Existing instructions outside the managed block stay intact.
`init --no-instructions` lets a repository maintain its own protocol integration.
`deploy` is separate from initialization so instructions can be reviewed first.
Instruction changes after adoption require normal events and bindings.

The baseline inventories source bytes with unknown provenance. It does not claim
to know requirements or why the code was written. An agent can then inspect one
area at a time, run `explain` and `history`, examine tests and documentation, and
introduce intents and bindings through normal metadata-only events. State whether
the rationale comes from a human requirement, written evidence, or an inference.
Record remaining uncertainty explicitly in rationale and verification evidence.
`report` and `report --json` expose both recorded rationale and unbound byte counts.
Reports include resolved current locations and explicitly labeled historical pins with snapshot IDs. A missing live target is shown as unavailable, never as a current location. Binding coverage is structural; even a fully bound file may have false rationale.

## Changes without intent updates

`audit` works before initialization and never writes to the repository. After
initialization it validates every reachable commit by default and validates the
worktree against HEAD. It also lists staged, unstaged, renamed, and untracked files,
plus bindings with unresolved status or outdated intent revisions. It does not
read untracked file contents. Corrupt records and unsupported operations fail
rather than reporting a clean audit. Shallow history is rejected. `--since BASE`
restricts historical checks to commits after an explicitly selected ancestor;
use a trusted checkpoint, since earlier gaps are outside that scope.

Each audit uses the HEAD captured at its start for both history and working-tree
validation, and checks that HEAD and the working snapshot remain unchanged before
returning, including on cached polls. A concurrent commit or edit causes a retry
diagnostic rather than a report that combines different repository states.
Invalid working metadata is included alongside any historical errors already
found. `derived_state_available: false` means the adoption backlog and binding
assessment lists could not be computed; empty lists then do not imply coverage.
`reconcile` still returns its task packet for investigating those errors.

Historical `SCHEMA_DEFINITION` pin mismatches appear in `historical_schema_drift`,
an informational category: reinstalling an engine must not label an old schema pin
as a bad source change. The audit still checks those records and transitions with
the running engine's trusted schemas; it never executes historical repository code
or trusts a repository-provided schema. Other structural errors remain failures.
Current worktree/staged validation and commit/push enforcement keep strict schema
pinning. This is a compatibility audit, not certification by each historical engine.
Outdated intent assessments still need review even when no structural errors remain.

`watch` polls in the foreground and emits only changed reports. It does not stage,
commit, run agents, or repair metadata. Each watcher caches immutable commit results
and its last exact worktree/metadata snapshot in memory under the loaded engine.
New HEADs, source, metadata, or merge state invalidate the relevant cached result;
restarting the process clears the cache. No cache is written into the repository.
The interval is a delay after each completed audit, not a fixed polling deadline;
the initial scan and changed snapshots may still be expensive. Transient read/Git
errors are emitted as attention states and retried on the next tick.
`watch --once` performs one check and exits nonzero for an error.
`reconcile` emits the audit and an agent task: inspect evidence, propose explicit
transitions, validate, and present the reasons for review. It can run on a machine
without any AI provider configured. Callers choose which agent consumes the JSON.

For uncommitted changes, an agent stages the selected source, captures an event,
then introduces or revises intent and declares each affected binding transition.
Never infer that unchanged behavior proves preserved intent. Use verification
states to distinguish a claim from evidence. New after-bytes always need bindings;
unknown provenance covers only unbound legacy before-bytes.

For changes already committed while hooks were bypassed, audit attaches commit IDs
to violations. A metadata-only follow-up may document current understanding but
cannot repair that commit's missing event chain. Repair an unmerged branch using
normal review and explicit events before merging. Do not rewrite committed records,
refresh the immutable baseline, or silently bless the current implementation.
Automatic historical repair is deliberately not implemented.

## Installation and hooks

The installer copies a private standard-library engine and protocol under the
chosen prefix and installs a single launcher. Git and the installation interpreter
must remain available. It refuses unmanaged destination collisions. Run uninstall
hooks in each adopted repository before removing or moving the installation. Hook
dispatchers retain original hooks, honor hooksPath, and invoke the installed engine
without assuming a `tools/` directory in the target repository. Reinstalling in the
same prefix upgrades the engine; `deploy --update` separately updates a managed
protocol, leaving the repository's committed instructions under its review policy.
Run the installed `intentctl init` when migrating from copied tools or changing
installation paths. It refreshes older managed hook dispatchers while preserving
the original hook backups and keeping identical dispatchers unchanged. Removing
tracked copied tools still requires normal source-change accounting.
Uninstall warns before deleting files. Orphaned strict hooks fail closed with the
missing engine/interpreter path and a recovery instruction: reinstall at the same
prefix, then run `intentctl uninstall-hooks` in each affected repository. There is
no global inventory of adopted repositories, so uninstall cannot safely find and
rewrite every repository's hooks.

Local hooks and local installed tools are convenience enforcement. Trusted CI must
use a reviewed tool version. Structural validation cannot prove intent truthfulness;
humans should review reasons, requirements, applicability, and verification evidence.

## Reconstruct historical intent

`intentctl trueup` can adopt an uninstrumented project and import evidence-cited
reconstruction from Git history and optional forge discussions directly into actual
`.intent` records. Normal `explain`, `history`, and `report` expose that context to
agents, including original commits, uncertainty and pending verification. Historical
bindings do not silently impose present-day constraints or erase the unknown
provenance backlog reported by `audit` and `reconcile`. Processing checkpoints live
in ignored `.intent/cache/trueup/`; the imported data survives deleting that cache.
See [true-up](trueup.md).
