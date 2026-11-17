# Data model, version 1

Published JSON Schemas live in `.intent/schema/`. Runtime validation supports exactly their keywords; schemas must byte-match the definitions shipped with the engine. External schema libraries are unnecessary. The standard `$schema` URI is documentation, not a network dependency.

## Canonical encoding and identity

Canonical content is UTF-8 JSON with lexicographically sorted object keys, compact separators, preserved array order, and no ASCII escaping of Unicode. Floats, NaN, duplicate object keys, invalid Unicode, and unknown record fields are rejected. Strings are not Unicode-normalized. Files add exactly one newline; the newline is excluded from content hashing.

`content_hash = SHA256(canonical(record without content_hash))`. Predecessor hashes pin immutable revision chains. Intent, binding, and event IDs are a type prefix followed by `SHA256(canonical([repository_namespace, object_type, creation_key]))`. The initial namespace is SHA-256 of the staged source inventory. Creation keys are explicit user inputs, not timestamps or random values. Separate cloned branches share a namespace.

The same key deliberately denotes the same identity; distinct concurrent creations MUST use distinct keys. Conflicting same-ID revisions produce normal Git conflicts or explicit collision failures. Different local writers must serialize operations; no distributed allocator or automatic conflict resolution exists.

## Records

| Record | Canonical location | Role |
| --- | --- | --- |
| Intent | `intents/I-…/000001.json` | Stable semantic identity, revision, active/retired/superseded lifecycle, kind, title, description, rationale, requirements, constraints, related IDs, optional severity, origin, predecessor hash |
| Binding | `bindings/B-…/000001.json` | Intent identity and assessed revision, relationship, target, rationale, predecessor binding IDs, active/ended/unresolved/historical lifecycle, origin, predecessor hash |
| Event | `events/E-….json` | Logical change, rationale, source states and intervals, declarations, verification, owned revisions, provenance |
| Source object | `objects/HASH.json` | Exact bytes as canonical base64, byte-content identity, own record hash |
| Manifest | `manifests/current.json` | Latest intent/binding revisions and ordered complete event list |
| Baseline | `baseline.json` | Immutable initial inventory explicitly marked unknown provenance |
| Config | `config.json` | Version, namespace, all-tracked-non-metadata coverage policy |

All paths above are relative to `.intent/`. Config and baseline are immutable after import. The manifest is mutable but cannot remove records or reorder committed events. Current heads must be the highest available revisions. Revisions start at 1 with null predecessor hash and advance without gaps.

Optional intent `severity` is `must`, `should`, or `prefer`. Absence means `must`, so records written before the field existed keep their meaning, and `must` is never written implicitly. Severity guides how agents weigh a conflict (see [writing intent](intent-authoring.md)); it does not change transition accounting, which the validator enforces identically for every severity.

Intent graph cycles are allowed. The current implementation uses `related_intents` for logical relationships; richer typed intent relationships remain future work. `superseded` lifecycle plus related IDs represents replacement, without claiming a typed replacement edge.

Binding relationship values are implements, constrained-by, motivated-by, preserves, verifies, and historically-derived-from. Current applicability is determined by binding status, independently of relationship wording. Use historical status for causation that no longer governs code. Unresolved status retains a visible constraint; further source edits must explicitly resolve or end it.

Optional binding `verification` separates agent claims from independent conclusions: `declared` (default when omitted on historical records), `pending`, `verified`, `failed`, or `unverifiable`. `verified` requires `method` and `evidence`. Event-level `verification` entries remain available for additional attestations.

## Command output contract

Canonical records carry `schema_version`. Every other JSON object printed by `intentctl`, including diagnostics, carries `output_version` (currently 1). Within a version fields are only added, never removed or repurposed, so agents can rely on documented keys. `tests/test_docs_commands.py` checks that documented commands and flags exist.

## Coordinates and snapshots

Targets contain path, start, end, source, and Git mode. Ranges are half-open byte intervals `[start,end)` in exact source bytes; empty ranges are valid insertion points. Paths are normalized relative POSIX paths, never absolute, dot segments, backslashes, or metadata paths. Source objects preserve CRLF, invalid text bytes, and missing final newlines unchanged.

Queries use one-based lines delimited by LF, with byte intervals derived from the selected snapshot. Byte offsets can identify subline expressions. An insertion touching either boundary of a binding is conservatively relevant. Empty files admit `[0,0)`. Active bindings are selected using live coordinates when equal-region mapping uniquely preserves pinned bytes onto the queried snapshot.

NUL-containing files and symlinks are opaque objects with whole-object ranges. Git mode changes are events even when bytes do not change. Symlink text is captured without following its target. Renames are deletion/addition with explicit rebinding; no Git rename heuristic establishes identity.

Events retain null before/after states for additions/deletions. `intervals` records exact deterministic byte diff opcodes for each transition. They demonstrate textual coverage, not semantic identity. Event source chains must begin at the base Git state and end at the proposed index.

## Explicit transitions

Each declaration identifies an intent, optional binding, action, old/new current intent revisions, and the binding revisions owned by the event. Use a null binding for an intent-only revision. Several declarations may reference the same intent revision when distinct bindings overlap.

- Introduce requires a new intent revision 1.
- Preserve, rebind, and unapply cannot revise the intent itself.
- Modify, retire, and supersede require a new intent revision; lifecycle actions must match its status.
- Unapply, retire, and supersede end referenced binding applicability.
- Preserve/rebind with source changes require successor binding revisions.

Old binding pins remain accessible after intent evolution. An `unknown` provenance declaration acknowledges missing coverage of unbound legacy before-bytes but cannot excuse any known binding's required transition and cannot leave newly introduced after-bytes unbound. `known` requires changed ranges to be covered on both existing sides of the event.
