# Lessons from Telos

[Telos](https://github.com/noahatfin/telos) is an "agent-first intent and constraint tracking layer for Git" with goals close to ours. This document records what a study of its code, design documents, experiments, and self-reviews (repository state `af76453`, March 2026) taught us, what we adopted, and what we deliberately declined. Behavior that follows from these lessons is implemented and tested in this repository; nothing below is borrowed on faith.

## Where the goals differ

| Question | Telos | This project |
| --- | --- | --- |
| Where does state live? | A separate `.telos/` object store with its own refs and streams, plus copies of constraints exported into `CLAUDE.md`/`AGENTS.md` by `telos sync` | Entirely in canonical JSON committed with the code. Nothing is cached, exported, or duplicated into instruction files |
| Who owns the interaction? | A human or agent runs a CLI; `sync` pushes constraints into agent memory so the agent notices them | The agent owns the whole loop (`AGENTS.md`): it queries, declares, and finalizes. The validator, not the agent's goodwill, rejects unaccounted changes |
| What is bound to code? | File or symbol name; `telos check --bindings` only tests that the file still exists | Exact byte ranges pinned to source snapshots, with explicit per-binding transitions |
| Is it enforced? | No. Constraints inform review | Yes. Hooks and CI reject changes whose affected bindings lack declarations |
| Agent identity | Caller-supplied, unauthenticated `agent_id`/`session_id` in `AgentOperation` records | Not recorded in events. Authorship is the Git commit; stronger identity needs signatures (roadmap) |

Because the goals differ, Telos is a source of evidence about what helps agents, not a design to copy.

## What Telos's evidence says

All figures are from Telos's own reports and are directional: small N, one model family (codex), LLM-as-judge.

- **Obvious violations need no help.** In the baseline run (N=2, 4 true-positive scenarios) agents with only Git history caught every violation (`3600 -> 86400`, default role `Admin`, ...). Context changed *citation* of the violated rule, not detection.
- **Violations a diff cannot show are where context pays.** In the hard-scenario run (N=5), Git-only agents caught 40% of a cross-module referential-integrity violation (archiving a board orphans its tasks) and 60% of a policy-value violation (`login_rpm: 100` against a recorded decision of `10`), and never cited the governing rule. With the rule supplied, detection and citation were 100% and reasoning quality rose from about 3.4 to about 4.9 of 5. Lock-removal style bugs (recognizable patterns) did not differentiate.
- **Supplying constraints did not reduce false rejections, and may have worsened them.** On benign changes (bugfix, cache, rename) agents rejected 50% of the time with Git only, 50% with a flat `CONSTRAINTS.md`, and 67% with Telos context.
- **A flat file was indistinguishable from the structured store** in the hard run (100% catch and citation for both; reasoning quality 4.8 vs 5.0 at N=5, which Telos itself calls not significant). Retrieval alone is not the differentiator.
- **Stale rules are the largest risk.** Telos's own design says the same property that makes accurate constraints useful (agents defer to them) makes stale ones harmful. Its fix is lifecycle states and active-only default queries.
- **Its earlier evaluation did not hold up to its own review.** Simulated responses, asymmetric prompts, keyword scoring, and no false-positive tests led its reviewers to recommend reframing "Strong Pass (6/7)" as proof of concept. In its one measured experiment (A), commit messages alone recovered 95% of the recall.
- **Its implementation reviews found** path traversal through stream names, no content-hash check on read, corrupted objects silently skipped by listing queries, integrity rules enforced only in the CLI, `context` doing one query per intent, last-writer-wins updates of a mutable ref, and documentation examples that drifted from the CLI.

## Decisions

| Lesson | Decision | Where |
| --- | --- | --- |
| Context must reach the agent when it decides, and rules diffs hide (policy values, cross-module invariants) need rationale and links | **Adopted.** `context PATH...` returns every governing intent with requirements, rationale, severity, and verification for a file or directory, ordered by severity, plus one-hop related intents bound elsewhere. `capture` now prints the same text beside each declaration it requires | `tools/intentlib/report.py`, `capture`, `tests/test_agent_queries.py` |
| Context without obligation levels invites over-rejection | **Adopted.** Optional per-intent `severity` (`must` default, `should`, `prefer`) and explicit agent guidance that the presence of an intent is not a violation; a benign edit declares only the bindings it touches | `schema.py`, `AGENTS.md`, `intent-authoring.md` |
| Staleness misleads | **Adopted and extended.** Queries default to active intent. `status`/`doctor` report intents that govern no code, bindings pinned to superseded revisions, unresolved bindings, and failed verification. `status --strict` exits nonzero | `report.freshness` |
| Know where nothing is recorded | **Adopted.** `coverage` reports bytes (not files) with no governing intent and lists wholly uncovered files; `context` lists uncovered regions per file as unknown provenance | `report.coverage` |
| Last-writer-wins on shared mutable state | **Adopted.** Mutating commands hold a per-worktree exclusive lock and fail fast; a multi-process test checks no draft update is lost and was mutation-checked against a lock-free build | `core.writer_lock` |
| Unversioned JSON contract and drifting docs | **Adopted.** Command results carry `output_version`; a test extracts every documented `intentctl` invocation and checks its subcommand and flags against the parser, and checks every subcommand is documented | `tests/test_docs_commands.py` |
| One scan per item (N+1) | **Adopted where it applied.** Validation repeated one quadratic whole-file diff per binding per event; the deterministic diff is now memoized per file pair | `core.opcodes` |
| Constraint authoring quality | **Adopted** as a guide adapted to this schema | `intent-authoring.md` |
| Evaluate with identical prompts, false-positive scenarios, a flat-file baseline, and a judge blind to condition | **Adopted as protocol.** No behavioral results are claimed here | `evaluation.md` |
| `telos sync` (copy constraints into `CLAUDE.md`/`AGENTS.md`) | **Declined.** It creates a second copy that drifts, and `AGENTS.md` is itself governed, so every intent edit would force a rebind. Agents are instead told to query. The state stays single-sourced | `AGENTS.md` |
| `AgentOperation` log with `context_refs` | **Declined for now.** An event with per-binding transitions already records what changed and which intents were accounted for. A self-reported agent id adds no trust; attribution waits for signatures | roadmap |
| Semantic search and embeddings (Telos phase 3) | **Declined.** Requires a model or index outside the repository | n/a |
| Hash check on read, no silent skipping of corrupt objects, path validation | **Audited, already satisfied for canonical records.** Records and source objects are hash-verified on load, a load error aborts every query, metadata paths and binding targets pass `safe_path`, and IDs are digests so keys cannot traverse directories | `core.Store`, `core.safe_path` |

## Residual risks and open questions

- Telos found that giving agents constraints is not enough. Whether the extra context, severity, and enforcement here change agent behavior is unmeasured. See [evaluation](evaluation.md) before claiming otherwise.
- Telos found a flat file matched structured retrieval. Our argument for structure rests on enforcement (a change cannot land without accounting), lifecycle, and byte-exact binding, not on retrieval quality.
- `status`'s decay signals are structural. They cannot tell that a requirement stopped being true.
- Validation cost still grows with file size because the deterministic diff is quadratic; memoization removed repeats, not the cost. See the [roadmap](roadmap.md).
