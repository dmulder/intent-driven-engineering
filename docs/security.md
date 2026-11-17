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

The implementation is an experiment, not a hardened sandbox. Working against a hostile checkout still exposes Git/Python/filesystem attack surfaces. Test jobs execute proposed code and must run without secrets or write credentials. The trusted PR job only executes base-branch tooling. No remote service is contacted by intentctl except through the optional `trueup` command, and only when the operator selects a forge network policy or an AI provider (see below).

Git history can be force-rewritten, hooks bypassed, and protected settings changed by administrators. This tool supplies consistency evidence relative to an accepted base, not cryptographic protection against a repository owner. First import explicitly admits unknown historical provenance.

Source snapshots may retain deleted secrets just as Git history does. Do not place secrets in source or intent records. A deliberate secret-removal migration may require rewriting protected history outside this append-only protocol; v1 provides no automated migration or erasure command.

## True-up (retrospective discovery)

`intentctl trueup` is the one component that reads untrusted text at scale: commit messages, historical code, forge comments, and model output. It is written on these assumptions.

| Threat | Mitigation | Remaining boundary |
| --- | --- | --- |
| Prompt injection in commits, PRs, comments or code | Evidence is delimited with a per-request nonce and labelled as data; output must validate against a schema; quotes must occur in cited evidence; fabricated references are dropped; provider output never reaches a shell | A model can still be persuaded to write a false but well-cited claim; promotion requires human review and records `pending` verification |
| Provider executes or edits things | Argument arrays, no shell, read-only flags where available, empty scratch working directory, scrubbed environment, hard timeout and process-group kill; model-suggested commands are never run | Third-party CLIs have their own behaviour and permissions |
| Credential leakage | Tokens come from named environment variables, are sent only to the configured origin, never follow cross-origin redirects, are not passed to providers, and are redacted from state and logs (including URL credentials and secret query parameters) | Pattern-based redaction cannot recognise every secret in code or text sent to a provider |
| Hostile repository | Git runs without a shell, hooks, external diff/textconv, lazy fetch, or protocols; repository content is never executed | Git itself remains attack surface |
| Hostile forge or response | Size, page, depth and count bounds; same-origin pagination only; negative caching | A compromised forge can supply false discussion |
| Data exposure to AI vendors | Explicit `--acknowledge-data-sharing`; `--no-ai` and `--network off` keep work local | The operator is responsible for the vendor's retention and training terms |
| Mistaking inference for fact | Separate store, mandatory labels, `basis`/`uncertainty` fields, lifecycle that describes history rather than current applicability, `historical` bindings by default | Consumers must keep treating it as leads |

True-up state is stored under the Git common directory, so no retrospective text enters `.intent/` unless a maintainer promotes a discovery. Promotion is metadata-only, records unknown provenance and `pending` verification, and goes through the ordinary validator.

Future independent verification should consume immutable event/record hashes and emit separately attributable evidence. Future signatures, protected attestations, reproducible checks, and transparency logs can strengthen provenance without turning agent claims into proof.
