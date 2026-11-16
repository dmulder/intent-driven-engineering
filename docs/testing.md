# Test strategy and review

Run `python3 -m unittest discover -s tests -v`. Tests require a local Git executable and Python 3.10+; they do not access the network. Every integration case creates and cleans up its own repository, identity configuration, CLI copy, index, and hooks. Global/system Git configuration is excluded. Unit tests check canonicalization against a fixed known SHA-256 value and ensure every published schema keyword is supported.

Integration assertions focus on externally observable results: accepted/rejected real commits, exact staged versus working bytes, retained revisions, overlap, lineage, reassessment, and diagnostics. Multi-step workflows are intentional for history and atomic-staging guarantees; they complement small failure-focused tests rather than replacing them. No mocked Git output or sleep-based synchronization is used.

Review of the initial tests identified and corrected these gaps:

- A stale binding diagnostic hid missing declarations. Failure cases now assert the missing transition code, and validation reports both.
- Assuming git revert invokes pre-commit would give false confidence. Tests use a proposed inverse, show its invalid metadata, and construct a compensating event; a separate pre-push test catches commits made while pre-commit was bypassed.
- A move/copy test did not establish split/consolidation behavior. A separate test now creates two successor bindings, then consolidates both with explicit lineage.
- Happy-path installation omitted existing pre-push stdin and hooksPath behavior. Dedicated cases check both, plus restoration on uninstall.
- Invalid schemas alone did not test tampering with published schema files, range declarations, or hidden manifest heads. Those are now separate rejection cases.

The review used the local review-testing skill. Outstanding test expansion includes crash injection during multi-file finalization, comprehensive malicious-input/resource-limit tests, macOS/Windows runners, SHA-256-format Git repositories, large-file benchmarks, and broader Git sequencer conflict recovery. Passing this suite is evidence for the documented prototype guarantees, not production readiness.
