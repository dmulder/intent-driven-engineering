# Test strategy and review

Run `python3 -m unittest discover -s tests -v`. Tests require a local Git executable and Python 3.10+; they do not access the network. Every integration case creates and cleans up its own repository, identity configuration, CLI copy, index, and hooks. Global/system Git configuration is excluded. Unit tests check canonicalization against a fixed known SHA-256 value and ensure every published schema keyword is supported.

Agent-surface tests build deterministic fixtures modeled on Telos's hard scenarios (a policy value with a recorded decision, a cross-module invariant) plus a benign-edit false-positive control, and assert the governing text is retrievable per file. They do not measure agent behavior; see [evaluation](evaluation.md). A multi-process test races six `bind` commands at one draft and asserts no accepted update is lost; it was mutation-checked by disabling the lock. A docs-as-test extracts every documented `intentctl` invocation and verifies its subcommand and flags against the parser, and that every subcommand is documented.

Integration assertions focus on externally observable results: accepted/rejected real commits, exact staged versus working bytes, retained revisions, overlap, lineage, reassessment, and diagnostics. Multi-step workflows are intentional for history and atomic-staging guarantees; they complement small failure-focused tests rather than replacing them. No mocked Git output or sleep-based synchronization is used.

Review of the initial tests identified and corrected these gaps:

- A stale binding diagnostic hid missing declarations. Failure cases now assert the missing transition code, and validation reports both.
- Assuming git revert invokes pre-commit would give false confidence. Tests use a proposed inverse, show its invalid metadata, and construct a compensating event; a separate pre-push test catches commits made while pre-commit was bypassed.
- A move/copy test did not establish split/consolidation behavior. A separate test now creates two successor bindings, then consolidates both with explicit lineage.
- Happy-path installation omitted existing pre-push stdin and hooksPath behavior. Dedicated cases check both, plus restoration on uninstall.
- Invalid schemas alone did not test tampering with published schema files, range declarations, or hidden manifest heads. Those are now separate rejection cases.

The review used the local review-testing skill. Outstanding test expansion includes crash injection during multi-file finalization, comprehensive malicious-input/resource-limit tests, macOS/Windows runners, SHA-256-format Git repositories, large-file benchmarks, and broader Git sequencer conflict recovery. Passing this suite is evidence for the documented prototype guarantees, not production readiness.

## True-up tests

The `tests/test_trueup_*.py` modules need no paid AI service and no live network. AI providers are replaced by `tests/fake_ai.py`, a deterministic command-line program driven by markers in commit messages and environment variables (flaky, malformed, fabricated, rate-limit, quota, slow, and failing replies), which the real provider layer invokes as a subprocess, so argument templating, stdin handling, timeouts, process-group kill and failure classification are exercised for real. Forge behaviour runs against a loopback HTTP mock (`tests/trueup_support.py`) covering pagination, rate limits, redirects to other origins, 404/403, injected 5xx, and credential-isolation checks; a transport guard fails any test that would contact a non-loopback host.

Coverage by module: Git scope (merges, roots, empty commits, shallow clones, tie-break order, missing objects, unusual path names, hostile repository configuration, read-only access to the target repository), providers and failure isolation (retry, fallback, circuit breaker, budgets, repair, oversize split), forge (reference forms, same-origin tokens, bounds, caching, secrets), durable state (fault injection at every write point and resume to the uninterrupted result, locking, versioning, reanalysis without overwrite), graph (lifecycle, merge gating, contested nodes), and `test_trueup_e2e.py`, direct canonical import, chronological revisions and historical blob pins, initial adoption, partial failure, idempotency, reanalysis, interrupted publication, cache deletion, clone portability and preservation of prior canonical bytes. Pipeline-focused tests use `--dry-run`; import integration tests exercise the public default. Normal queries are also checked without any analysis cache.
