# Canonical storage layout

True-up imports use immutable record batches and raw source snapshots. Ordinary
agent queries still return the same logical intents, revisions, bindings and
events. Record identities, hashes, revision chains and historical source bytes
retain their existing meaning. The processing cache remains disposable.

## Physical files

- `.intent/objects/<sha256>.blob`: exact source bytes. The name hashes those bytes.
  Git can reuse an identical blob already present in source history, and no base64
  expansion is required. Standalone snapshots remain available without looking up
  the original source commit. `.intent/.gitattributes` disables text conversion for
  these blobs and batch files; staged validation detects altered bytes.
- `.intent/records/<sha256>.json`: a canonical JSON batch with `format` equal to
  `intentctl-record-pack-v2`, a `records` map, and a `values` map. The filename hashes
  the canonical batch bytes excluding its final newline. An import publishes one
  batch instead of thousands of small intent/binding files.
- Existing manifests, baselines, configuration and published logical schemas keep
  their current paths. Hand-authored intent workflows retain individual JSON record
  files for reviewability. Readers support those files alongside imported batches.

`records` maps each logical record path to an encoded value. Only intent, binding,
and event paths are permitted; a batch cannot replace source, config or schemas.
An encoded value is `[0, literal]` or `[1, digest]`. Literals are never interpreted
as references, even when they happen to resemble storage syntax.

`values` maps SHA-256 digests to typed nodes. Nodes are canonical JSON objects:

- `object` has a `fields` map of encoded values.
- `list` has at most 32 encoded `items`.
- `list-branch` has at most 32 child digests and concatenates their lists.
- `text` has at most 1,024 Unicode characters in `value`.
- `text-branch` has at most 32 child digests and concatenates their strings.

Small values (at most 192 canonical bytes) stay inline. Larger arrays and strings
use bounded trees. As a history grows, full leaves and internal nodes are reused;
a revision carries a new root rather than copying an unbounded list of references.
Long cumulative rationale strings and requirement lists receive the same treatment
as embedded reconstruction history. Shared values may refer to older immutable
batches. Logical records are reconstructed before their existing content hashes,
references, origin events, coverage, and transition rules are validated.

## Publication and validation

The importer validates the proposed logical tree, saves its existing ignored
recovery journal, writes a batch atomically, and publishes the manifest last.
Retries complete the same transaction. Earlier batches and records are preserved.
Source blobs use atomic replacement with fsync. Existing legacy JSON snapshots
are reused when already present rather than duplicated in another encoding.

Readers verify batch and value digests, canonical encoding, allowed logical paths,
source hashes and conflicting duplicate representations. Missing references,
unknown node kinds, unused values, cycles, oversized fan-out and excessive depth
fail closed. Expanded records have a 64 MiB safety limit to reject compact
expansion bombs. Queries read current bytes; the importer can reuse a verified
record index within its serialized writer session, without a persistent index.

Old Git commits and mixed trees remain readable by the updated engine. Older
engines cannot read batches or raw snapshots and must not operate on new trees.
No storage conversion command or migration framework is part of this change.

## Growth and trade-offs

Cumulative embedded histories store prefixes of sizes 1, 2, ..., n, producing a
quadratic payload for a repeatedly revised intent. Bounded shared trees avoid
storing those full prefixes again. The cost of a small append is proportional to
its new values and changed tree paths, not the full accumulated history. New
unique evidence and source versions still consume space.

This is a disk-layout improvement, not a claim that all CPU and memory costs are
linear: the current logical reader materializes records and the importer still
constructs historical projections. A very long-lived intent can therefore remain
expensive to query or import even when its stored representation is compact.

Batches reduce allocation/inode overhead and metadata file scans. They are less
convenient to review as raw JSON; use ordinary `show-intent`, `history`, `explain`
and `report` queries for semantic review. Git can delta-compress similar batches,
but actual Git pack size must be measured separately from working-tree bytes.
Retaining old commits also retains their earlier storage representations.

Run the synthetic size/round-trip benchmark from this checkout:

```sh
python3 scripts/benchmark_storage.py --revisions 40 80 160
```

It compares complete logical revision payloads with a batch, verifies that every
revision reconstructs exactly, and prints sizes and encode time. It makes no model
or network calls. Synthetic results establish the sharing mechanism, not savings
for a particular repository.
