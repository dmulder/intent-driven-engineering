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
Binding coverage is structural; even a fully bound file may have false rationale.

## Changes without intent updates

`audit` works before initialization and never writes to the repository. After
initialization it validates every reachable commit by default and validates the
worktree against HEAD. It also lists staged, unstaged, renamed, and untracked files,
plus bindings with unresolved status or outdated intent revisions. It does not
read untracked file contents. Corrupt records and unsupported operations fail
rather than reporting a clean audit. Shallow history is rejected. `--since BASE`
restricts historical checks to commits after an explicitly selected ancestor;
use a trusted checkpoint, since earlier gaps are outside that scope.

`watch` polls in the foreground and emits only changed reports. It does not stage,
commit, run agents, or repair metadata. `watch --once` performs one check.
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

Local hooks and local installed tools are convenience enforcement. Trusted CI must
use a reviewed tool version. Structural validation cannot prove intent truthfulness;
humans should review reasons, requirements, applicability, and verification evidence.
