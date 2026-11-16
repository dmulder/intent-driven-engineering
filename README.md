# Intent-native Git overlay

An experimental, offline engineering provenance system. Code, intent, bindings, and logical events evolve independently; Git stores consistent snapshots of all four. The CLI and validator use Python's standard library and Git plumbing. No model, server, inline source annotations, or package installation is required.

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

## Install into an existing repository

Copy `tools/intentctl.py` and `tools/intentlib/` into the destination's `tools/` directory without replacing existing files. Copy or merge `AGENTS.md` into its operating instructions. Run from anywhere inside that repository:

```sh
git add tools AGENTS.md
python3 tools/intentctl.py init
# Stage any other files that belong in the import, then explicitly refresh inventory:
python3 tools/intentctl.py baseline
git add .intent
python3 tools/intentctl.py validate --staged
git commit -m 'Import intent system with unknown legacy provenance'
```

The first transaction is an **unknown-provenance baseline only**. Introduce verified intent in a subsequent transaction. All tracked files outside `.intent/` are governed, including documentation, tests, hooks, and this tool. Working-tree validation includes tracked source files and untracked canonical metadata; untracked source must first be staged.

`init` creates configuration, schemas, an immutable baseline inventory, a manifest, and hook dispatchers. It preserves existing hooks and respects `core.hooksPath`. Repeating it does not replace metadata. `init --no-hooks` initializes metadata only. `uninstall-hooks` restores original hook files; it does not delete history. [Hook details](docs/git-integration.md).

## Record independent intent

The following creates a metadata-only intent and binds it to an existing file. Commands return JSON objects containing generated IDs; substitute the returned IDs for `I-ID` and `B-ID` below. IDs are full SHA-256 digests prefixed by their object type, not the short illustrative identifiers in the assignment.

```sh
python3 tools/intentctl.py capture --key valid-tokens \
  --description 'Introduce token validity policy' \
  --rationale 'Authentication requires acceptable credentials' --provenance unknown
python3 tools/intentctl.py intent-create --event valid-tokens --key valid-tokens \
  --title 'Token validity' --description 'Reject invalid tokens' \
  --rationale 'Invalid credentials must not grant access' \
  --requirement 'Accept only valid tokens'
python3 tools/intentctl.py bind --event valid-tokens --key token-condition \
  --intent I-ID --path auth.py --start 34 --end 45 \
  --rationale 'This expression checks token validity'
python3 tools/intentctl.py declare --event valid-tokens --intent I-ID \
  --binding B-ID --action introduce
python3 tools/intentctl.py finalize valid-tokens
git add .intent
python3 tools/intentctl.py validate --staged
git commit -m 'Introduce token validity intent'
```

Ranges are half-open UTF-8 **byte offsets**, not character positions. Omitting `--start` and `--end` binds the whole file. Bind another intent to the same bytes to represent overlap. Bind the same intent to another region using another binding key.

## Modify governed code

```sh
# Make and stage the intended source edit first.
git add auth.py
python3 tools/intentctl.py capture --key refactor \
  --description 'Extract authentication helper' \
  --rationale 'Centralize eligibility checks' --provenance unknown
python3 tools/intentctl.py rebind B-ID --event refactor --path auth.py \
  --start 34 --end 75 --rationale 'Policy is implemented in the helper'
python3 tools/intentctl.py declare --event refactor --intent I-ID \
  --binding B-ID --action preserve
# Repeat rebind + declare for EVERY affected binding, including overlapping ones.
python3 tools/intentctl.py finalize refactor
python3 tools/intentctl.py validate
git add .intent
python3 tools/intentctl.py validate --staged
git commit -m 'Extract authentication helper'
```

Capture generates `.intent/drafts/E-….json` and a list of required declarations. It **never declares preservation**. Edit draft JSON for verification evidence or advanced many-region records; helpers cover common intent and binding operations. `declare` replaces an earlier declaration for the same intent/binding pair, so run it after preparing the corresponding revisions.

`--provenance unknown` acknowledges unbound *legacy before-bytes*; it does not waive known-intent accounting and cannot introduce unbound after-bytes. Newly introduced bytes always require at least one binding (`NEW_CODE_REQUIRES_INTENT` otherwise). `known` requires bindings to cover changed bytes in both existing source states. Leaving provenance unset prevents finalization.

Bindings that do not overlap a changed interval are not required to rebind. Their pins may remain on older snapshots when deterministic equal-region mapping uniquely preserves their bytes (coordinate maintenance, not semantic inference). Ambiguous moves still need an explicit `rebind`. Use `--verification-status` on bind/rebind to separate claimed preservation from independently supported verification.

For an intent-only change, capture with no staged source edits, use `intent-revise I-ID --event KEY --requirement TEXT`, and declare `modify`. Old bindings remain pinned to their exact revision and appear in `status`/`impact` as needing reassessment. Use `rebind --intent-revision N` in a later event to acknowledge the new policy.

## Query and diagnose

```sh
python3 tools/intentctl.py status --json
python3 tools/intentctl.py explain auth.py:2 --json
python3 tools/intentctl.py explain auth.py:2 --at HEAD~1 --json
python3 tools/intentctl.py history auth.py:2 --json
python3 tools/intentctl.py show-intent I-ID --revision 1 --json
python3 tools/intentctl.py show-binding B-ID --json
python3 tools/intentctl.py impact I-ID --json
python3 tools/intentctl.py doctor --json
python3 tools/intentctl.py validate --commit HEAD
python3 tools/intentctl.py validate --range BASE..HEAD
```

Human output is indented JSON for records, and concise diagnostics for validation. JSON validation returns `valid`, `errors`, stable codes, paths, and messages; failure exits nonzero. `doctor` diagnoses without modifying history. Repair drafts or append corrective records; never overwrite committed revisions.

## Design and status

- [Architecture and tradeoffs](docs/architecture.md)
- [Data model and canonicalization](docs/data-model.md)
- [Validator specification](docs/validator.md)
- [Git support and limitations](docs/git-integration.md)
- [Security and trust](docs/security.md)
- [Reproducible demonstration](docs/demonstration.md)
- [Roadmap](docs/roadmap.md)
- [Mandatory agent protocol](AGENTS.md)

Canonical history is versioned JSON. Drafts and future caches are disposable. Source snapshots intentionally trade storage efficiency for durable intermediate states independent of commit boundaries. V1 scans metadata and uses explicit lineage; it has no AST matching, automatic identity inference, or persistent code-fragment IDs.

Licensed under [MPL-2.0](LICENSE).
