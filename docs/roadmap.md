# Roadmap and implementation decisions

## Delivered foundation

- [x] Independent immutable intent and binding revisions, append-only events, canonical JSON and source objects.
- [x] Explicit per-binding transitions, overlapping applicability, exact indexed-source enforcement and Git hooks.
- [x] Intent-only evolution, impact/reassessment reporting, current and historical queries.
- [x] Explicit move/copy/split/consolidation lineage; no automatic fragment identity claims.
- [x] Unknown baseline import, auditable one-time bootstrap, CI, real-Git tests and A–H demonstration.
- [x] Region-aware affected bindings with deterministic equal-region coordinate maintenance (no file-wide rebind tax).
- [x] Stricter unknown provenance: newly introduced after-bytes require intent coverage.
- [x] Binding verification states separate claimed preservation from independently supported conclusions.

## Before production adoption

1. Benchmark large histories, repetitive files, and many bindings. Add a bounded byte-diff strategy and incremental object/record reads. Cache interval and graph indexes only as reproducible derived data.
2. Implement an explicit multi-parent reconciliation event and divergent revision model. Numeric revision conflicts currently require branch repair or rejection.
3. Add transactional recovery/journaling for finalization and locking for concurrent writers in a worktree. Independent worktrees already isolate normal writes.
4. Add complete resource limits and hostile-input testing. Verify installation on macOS and Windows; shell hook execution currently targets Linux.
5. Add typed intent relationships and richer independently verified evidence with hashes, attestations, and signatures.
6. Introduce first-class code fragment identities that survive moves/splits without replacing binding identity; keep location queries distinct from fragment/binding history queries.
7. Reduce snapshot duplication by referencing Git blobs where a commit already stores the bytes, retaining overlay objects for intermediate states Git does not.

## Open architectural questions

A persistent fragment ID may coexist with exact source snapshots and explicit many-to-many lineage, but must not hide split/copy ambiguity. V1 binds exact snapshots, maintains live coordinates when equal regions uniquely preserve bytes, and delegates logical identity to declared binding continuity.

Bindings intentionally reference both logical intent identity and exact assessed revision. Automatically following the latest revision would erase the distinction between a changed requirement and acknowledged implementation compliance.

`preserve` remains an agent declaration. Verification status on the binding records whether that claim is merely declared, pending review, verified with method/evidence, failed, or unverifiable. Structural validation still cannot prove behavioral equivalence.

Logical events have no fixed granularity. They must connect exact states and account for constraints, whether they represent one byte, multiple files, or metadata alone. Event-first authoring could become the primary engineering workflow without replacing Git now.

Trusted provenance requires more than hashes: protected baselines, independent execution, signed evidence, accountable reviewers, and eventual distributed reconciliation are separate problems. False preservation remains the principal semantic risk.
