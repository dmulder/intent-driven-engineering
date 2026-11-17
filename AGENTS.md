# Agent instructions

<!-- intentctl:begin -->
Read [INTENT.md](INTENT.md) before investigating or changing this repository.
<!-- intentctl:end -->

For development, run the CLI from this checkout with `python3 tools/intentctl.py`
or install this checkout using `make install`. Run `make test` for tool changes.

Before edits, run `python3 tools/intentctl.py audit --json` and use this checkout's CLI for `explain` and `history` on affected regions. Follow INTENT.md throughout.

Run `python3 tools/intentctl.py context PATH --json` before changing an affected file or directory. Use `python3 tools/intentctl.py list-intents --contains TEXT --json` to locate governing intent when the path is unknown.

Intent severity defaults to `must`: conflicting changes require an authorized intent revision. Deviations from `should` need a recorded rationale naming the intent; `prefer` is advisory. Compare changes with the requirement text and cite the intent ID when reporting a conflict. Unbound bytes have unknown provenance.

Treat `status`/`doctor` attention items as structural decay signals to investigate; they do not prove a requirement has become false.
