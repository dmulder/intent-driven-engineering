# Git integration and limitations

## Hooks

`init` resolves the effective hooks directory through `git rev-parse --git-path hooks`; it does not change `core.hooksPath`. Existing pre-commit/pre-push files are renamed to `.intentctl-original` and chained first if executable. Their original arguments and pre-push stdin are preserved. Managed dispatchers locate the current worktree at execution time. Relative hooksPath configurations retain Git's semantics. A hooks directory shared across unrelated repositories also shares these dispatchers: initialize only where all affected repositories have the CLI, or configure a dedicated directory yourself.

Symlink hooks and ambiguous backups are rejected without overwrite. Installation is idempotent. `uninstall-hooks` removes only marked dispatchers and restores original hooks. Hook installation across both files is not atomic; inspect/retry after a failure. Never run `init` from an untrusted repository without reviewing the tool.

The checked-in `hooks/` scripts are templates; generated dispatchers provide chaining. Commit messages carry no intent semantics. Python must be available as `python3` on PATH.

| Scenario | V1 behavior |
| --- | --- |
| Staged / unstaged source | Index validation reads only indexed bytes; working validation checks tracked on-disk files |
| Partially staged files | Supported; bindings/capture target indexed content |
| Unstaged metadata | Cannot satisfy pre-commit |
| Additions, deletions, empty files, mode changes | Supported through explicit events |
| Renames / moves / copies | Delete/add or new file plus explicit binding revision/predecessor declarations |
| Binary / symlink | Opaque whole-object ranges; symlinks are not followed |
| Empty commit | Valid if existing state is consistent |
| Merge commit | Rejected; rebase or squash instead |
| Squash | Valid when concatenated event chains connect base to resulting source and immutable histories agree |
| Rebase / cherry-pick | Valid for compatible source chains and nonconflicting metadata; no automatic repair |
| Revert | Raw inverse metadata deletion is invalid; make a compensating source event retaining history |
| Detached HEAD | Supported |
| Linked worktrees | Each uses its own index/HEAD; hooks can be shared; mutating commands take a per-worktree lock (`intentctl.lock` in the worktree's Git directory) and fail fast when another writer is active |
| Concurrent branches | Distinct keys coexist; same-ID conflicting revisions require explicit reconciliation |
| Metadata conflicts | Invalid JSON/unmerged index rejected; resolve deliberately |
| Submodules / non-UTF-8 filenames | Explicitly rejected |
| Untracked source | Stage before capture; working validation does not inventory it |
| Sparse checkout / index extensions | No special support promised; exact-index workflows only |

Git sequencer operations may not call pre-commit. A local `git revert`, cherry-pick, or rebase can therefore create an invalid commit before a hook runs. Pre-push and CI inspect introduced commits and reject inconsistencies. Hooks are bypassable, so protected CI is necessary.

## Reverts and repair

To reverse an implementation, use `git revert --no-commit COMMIT`, restore `.intent` from HEAD in both index and working tree, and capture the inverse source as a new event. Revise bindings and declare transitions normally. Preserve original rationale and record the reason for the reversal. Do not claim the inverse automatically reinstates old intent lifecycle.

For divergent same-ID revisions, do not renumber committed files in place. Rebase one branch onto the other, retain the first branch's records, and regenerate the unmerged branch's logical changes as new revisions/events with explicit reconciliation rationale. If both conflicting histories are already protected, v1 cannot merge them: retain both branch tips as archival Git history and perform a maintainer-reviewed migration before proceeding.

## CI

The regular workflow runs tests and validates introduced commits using the proposed engine. A separate `pull_request_target` workflow runs ONLY the trusted base engine against the PR's index/history. It never executes PR scripts, tests, hooks, or dependency installers. Checkout disables credential persistence; token permissions are read-only. The trusted engine must already exist on the base branch; first adoption requires an explicitly reviewed baseline.

Configure required checks and protect workflow files, validator changes, and the trusted base branch. Proposed workflow edits cannot be trusted to enforce themselves. Repository/organization rulesets and independent review remain administrative prerequisites, not capabilities supplied by this CLI. A base engine will reject unsupported future schema versions until a trusted upgrade is deployed.
