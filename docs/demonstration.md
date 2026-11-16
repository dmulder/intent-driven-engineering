# Reproducible demonstration

From the prototype checkout:

```sh
python3 examples/demo.py /tmp/intent-demo
```

Choose a destination that does not exist. The script copies the CLI, initializes an ordinary Git repository, establishes an explicit unknown baseline, installs hooks, and executes the full scenario with assertions. It requires no network and leaves the resulting repository for inspection.

Expected progress:

```text
A: introduced token-validity intent
B: two independent intents overlap on auth.py:2
C: helper refactor explicitly preserved both intents
D: intent revision 2 changed no source; token binding needs reassessment
E: implementation updated; both bindings revised; lockout intent unchanged
F: history reconstructed 5 events and token intent revisions 1 and 2
G: missing lockout declaration rejected; Git hook also rejected commit
H: supplied missing declaration; hook accepted commit
```

The final JSON reports the generated intent IDs, initial/final Git commits, and six logical events. Commit hashes may vary with Git author timestamps; canonical intent metadata contains no timestamps and is deterministic for identical staged inputs and creation keys.

Inspect overlap and provenance:

```sh
cd /tmp/intent-demo
python3 tools/intentctl.py explain auth.py:2 --json
python3 tools/intentctl.py history auth.py:2 --json
python3 tools/intentctl.py status --json
python3 tools/intentctl.py validate --staged
python3 tools/intentctl.py validate --range HEAD~6..HEAD
```

`explain` returns two applicable bindings, with token intent revision 2 and account-lockout revision 1. `history` returns six events plus every related intent/binding revision, including the original condition and later helper snapshots. `status` reports no reassessment after step E. Use the reported `initial_commit` with `explain auth.py:2 --at COMMIT --json` to retrieve both original revision-1 intents.

Step G first prepares only the token declaration. Finalization identifies MISSING_TRANSITION for the lockout binding. Since invalid metadata cannot be finalized, committing the staged edit then fails pre-commit with UNCOVERED_CHANGE. Step H adds the lockout binding revision/declaration, finalizes, stages the metadata, and successfully commits. This demonstrates two independent enforcement boundaries rather than writing a known-invalid canonical event.

The same scenario is exercised by `tests/test_demo.py`; the rest of the suite covers partial staging, retirement, supersession, moves/copies, and compensating reverts.
