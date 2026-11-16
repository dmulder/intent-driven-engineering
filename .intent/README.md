# Canonical intent history

JSON records in this directory are independent engineering entities, not annotations owned by lines or commits. See ../docs/data-model.md and ../AGENTS.md.

Intents and bindings have stable IDs and immutable numbered revisions. Events are append-only. Source objects retain exact intermediate bytes. The current manifest selects heads without erasing history. The baseline explicitly records unknown legacy provenance.

Never edit a committed revision or event. Prepare drafts with `python3 tools/intentctl.py capture`, declare transitions, finalize, stage metadata with source, and validate the exact index. `drafts/` and `cache/` are disposable and must remain untracked.
