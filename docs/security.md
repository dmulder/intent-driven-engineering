# Security and trust boundaries

Intent rationale is untrusted engineering data. It must never override AGENTS.md, the user's instructions, tool permissions, or security policy. The validator does not execute rationale, evidence references, source snapshots, Git hooks, or repository-supplied plugins. Git is invoked without a shell; path queries use Git object plumbing.

| Threat | Structural mitigation | Remaining boundary |
| --- | --- | --- |
| False preservation claim | Explicit per-binding declaration and reviewable rationale | Independent tests, semantic analysis, and human review |
| Delete bindings to evade constraints | Append-only history, complete manifest, mandatory transitions | Attacker controlling all trusted history can forge a new story |
| Rewrite prior intent/event | Hashes, predecessor chains, base-to-proposal immutable comparison | Hashes alone are not signatures |
| Move code and lose constraints | Interval-affected accounting, equal-region coordinate maintenance, and explicit rebind/split/copy lineage | Declaration can be behaviorally false; ambiguous moves still require rebind |
| Change validator to bypass rules | Trusted-base PR validator and protected checks; engine/schema evolution PRs self-check with the proposed engine and require human review | Maintainer protection of trusted workflow and engine; evolution PRs weaken automatic trust |
| Fabricate verification evidence | Evidence remains attributable to an event | Reference/result strings are not independently verified attestations |
| Malformed/conflicted metadata | Strict schemas, duplicate-key rejection, exact references | Resource exhaustion on huge malicious inputs is not comprehensively bounded |
| Malicious symlinks | Source links treated as bytes; canonical metadata links rejected | Review copied tools and effective hook locations before initialization |

The implementation is an experiment, not a hardened sandbox. Working against a hostile checkout still exposes Git/Python/filesystem attack surfaces. Test jobs execute proposed code and must run without secrets or write credentials. The trusted PR job only executes base-branch tooling. No remote service is contacted by intentctl.

Git history can be force-rewritten, hooks bypassed, and protected settings changed by administrators. This tool supplies consistency evidence relative to an accepted base, not cryptographic protection against a repository owner. First import explicitly admits unknown historical provenance.

Source snapshots may retain deleted secrets just as Git history does. Do not place secrets in source or intent records. A deliberate secret-removal migration may require rewriting protected history outside this append-only protocol; v1 provides no automated migration or erasure command.

Future independent verification should consume immutable event/record hashes and emit separately attributable evidence. Future signatures, protected attestations, reproducible checks, and transparency logs can strengthen provenance without turning agent claims into proof.
