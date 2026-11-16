# Deterministic validator specification

## Inputs

Pre-commit uses HEAD and the exact Git index. An unborn repository has an empty base. `validate` without `--staged` uses tracked working-tree source and on-disk metadata. It deliberately does not discover untracked source. `--commit REV` compares a commit with its parent. `--range A..B` validates every introduced commit in topological order, rather than only the net diff.

Machine-readable Git commands (`ls-files --stage -z`, `ls-tree -rz`, `cat-file`, and `rev-list`) preserve exact filenames and bytes. Unmerged stages, submodules, non-UTF-8 paths, and merge commits fail explicitly. Git object SHA-1/SHA-256 format does not affect the overlay's SHA-256 fingerprints.

## Algorithm

1. Parse every proposed canonical record. Reject noncanonical serialization, invalid schemas, missing schema definitions, and malformed source objects.
2. Compare committed canonical records with the base; reject modifications or deletion. Only the manifest can advance. Config and baseline cannot change after import.
3. Validate record paths, identity types, hashes, revision chains, origin-event ownership, references, complete heads, complete event membership, targets, ranges, and lifecycle consistency.
4. Require old manifest events to remain an identical prefix. Initialize source and current binding/intent maps from the base.
5. Replay each new event. Its per-file before state must match the preceding state; exact declared diff intervals must match its retained source snapshots.
6. Require explicit transitions and successor revisions for all active/unresolved pre-event bindings in every changed file. Validate action-specific intent revision, lifecycle, and binding ownership rules. An unresolved binding cannot remain unresolved through another affected code edit.
7. Require every owned intent or binding revision to have a declaration. Advance revision heads in order. A metadata-only event advances the semantic maps without changing source.
8. Apply source changes and check known/unknown provenance declarations. Unknown provenance never suppresses known binding accounting.
9. Compare the final replayed source with the proposed source: any difference is an uncovered change. Check current active targets against exact final snapshots. Report stale binding pins to newer intents as reassessment needs through query commands; they are valid states.

Initial adoption is separate: a new unknown-provenance baseline must exactly inventory the proposed source. That first transaction cannot contain intents, bindings, or events. Previously uninstrumented Git commits are not retroactively declared covered.

## Diagnostics and guarantees

Validation exits 0 on success and 1 on structural failure. JSON contains `valid` and `errors`; errors carry code, path, message, and affected intervals when available. Examples include UNCOVERED_CHANGE, MISSING_TRANSITION, STALE_TARGET, IMMUTABLE, REFERENCE, REVISION_CHAIN, SOURCE_CHAIN, SCHEMA, and MERGE_UNSUPPORTED. CLI operation failures use OPERATION and include the underlying diagnostics. Stable sorting makes repeated validation outputs deterministic.

The engine checks structural accountability, not truthfulness of rationale, behavioral equivalence, satisfaction of a requirement, or validity of external evidence. Full byte equality is snapshot evidence, never sufficient proof of semantic continuity.

## Cost

V1 loads the current canonical history and exact Git snapshots. It builds current-head maps and replays only new events during a transaction. There is no persistent index; historical queries are lazy in the sense that they do not scan Git commits, but they do scan retained records. Byte SequenceMatcher may be expensive for large repetitive files. Full snapshots duplicate Git content, though overlay objects deduplicate by hash. This prototype establishes correctness semantics, not million-line performance claims. See the roadmap for incremental validation and bounded diff algorithms.
