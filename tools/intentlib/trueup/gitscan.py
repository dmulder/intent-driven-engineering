"""Read-only Git history enumeration and commit inspection through plumbing.

No shell is used. The target working tree and index are never touched: every command reads
objects only. Repository-controlled external diff drivers and textconv filters are disabled.
"""
import heapq
import os
from pathlib import Path
import re
import subprocess
import threading

from .util import TrueUpError, path_text, safe_text

GIT_ENV_OVERRIDES = {
    "GIT_OPTIONAL_LOCKS": "0",       # never refresh the index
    "GIT_NO_LAZY_FETCH": "1",        # partial clones must not trigger network fetches
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "true",
    "LC_ALL": "C",
}
GIT_GLOBAL_ARGS = ["-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "protocol.allow=never",
                   "-c", "core.hooksPath=" + os.devnull, "-c", "log.showSignature=false"]
HEX = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class GitFailure(TrueUpError):
    def __init__(self, message, returncode=None, stderr=""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                     "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def git_env():
    env = dict(os.environ)
    for name in ("GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_PAGER", "PAGER", *GIT_LOCATION_VARS):
        env.pop(name, None)
    env.update(GIT_ENV_OVERRIDES)
    return env


class Repo:
    def __init__(self, path):
        self.path = str(path)
        probe = self.run("rev-parse", "--git-dir", "--git-common-dir", "--is-bare-repository", ok=(0,), fatal="not a Git repository: " + self.path)
        lines = probe.decode().split("\n")
        base = Path(self.path)
        self.git_dir = (base / lines[0]).resolve()
        self.common_dir = (base / lines[1]).resolve()
        self.bare = lines[2].strip() == "true"
        top = self.run("rev-parse", "--show-toplevel", ok=(0, 128))
        self.toplevel = Path(top.decode().strip()).resolve() if top.strip() else None

    def run(self, *args, input=None, ok=(0,), timeout=300, fatal=None):
        argv = ["git", *GIT_GLOBAL_ARGS, "-C", self.path, *args]
        try:
            result = subprocess.run(argv, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=git_env(), timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise GitFailure(f"git {args[0]} timed out after {timeout}s") from exc
        except FileNotFoundError as exc:
            raise TrueUpError("git executable not found") from exc
        if result.returncode not in ok:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise GitFailure(fatal or f"git {' '.join(args[:3])}: {stderr}", result.returncode, stderr)
        return result.stdout

    def run_capped(self, *args, cap, timeout=300):
        """Run git and return (bytes, truncated) keeping at most `cap` bytes of stdout in memory."""
        argv = ["git", *GIT_GLOBAL_ARGS, "-C", self.path, *args]
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git_env())
        timer = None
        try:
            timed_out = threading.Event()

            def expire():
                timed_out.set()
                process.kill()

            timer = threading.Timer(timeout, expire)
            timer.start()
            data = process.stdout.read(cap + 1)
            truncated = len(data) > cap
            if truncated:
                process.kill()
                data = data[:cap]
            else:
                process.wait()
            if timed_out.is_set():
                raise GitFailure(f"git {args[0]} timed out after {timeout}s")
            if not truncated and process.returncode != 0:
                raise GitFailure(f"git {args[0]} failed with exit status {process.returncode}", process.returncode)
            return data, truncated
        finally:
            if timer:
                timer.cancel()
            process.stdout.close()
            try:
                process.kill()
            except OSError:
                pass
            process.wait()

    def missing_commits(self, shas):
        """Which of `shas` are absent from the object database (one process for any number of ids)."""
        if not shas:
            return []
        out = self.run("cat-file", "--batch-check", input=("\n".join(shas) + "\n").encode(), timeout=600).decode()
        return [line.split()[0] for line in out.splitlines() if line.endswith(" missing")]

    def path_exists(self, revision, path):
        return subprocess.run(["git", *GIT_GLOBAL_ARGS, "-C", self.path, "cat-file", "-e", f"{revision}:{path}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env()).returncode == 0

    def object_exists(self, oid):
        return subprocess.run(["git", *GIT_GLOBAL_ARGS, "-C", self.path, "cat-file", "-e", oid + "^{commit}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env()).returncode == 0


def resolve_ref(repo, ref):
    if not ref or ref.startswith("-"):
        raise TrueUpError(f"invalid ref {ref!r}")
    out = repo.run("rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}", ok=(0, 1))
    oid = out.decode().strip()
    if not HEX.match(oid):
        raise TrueUpError(f"ref {ref!r} does not resolve to a commit in this repository")
    return oid


def resolve_scope(repo, refs=None, all_local=False, include_remotes=False):
    """Resolve starting refs to immutable commit ids. Default scope is the checked-out HEAD."""
    resolved, skipped = [], []
    if all_local:
        prefixes = ["refs/heads/", "refs/tags/"] + (["refs/remotes/"] if include_remotes else [])
        raw = repo.run("for-each-ref", "--format=%(refname)%00%(objecttype)", *prefixes).decode()
        for line in raw.splitlines():
            name, _, kind = line.partition("\x00")
            if kind not in ("commit", "tag"):
                skipped.append({"name": name, "reason": f"points to a {kind}, not a commit"})
                continue
            try:
                resolved.append({"name": name, "oid": resolve_ref(repo, name)})
            except TrueUpError as exc:
                skipped.append({"name": name, "reason": str(exc)})
    for ref in (refs or ([] if all_local else ["HEAD"])):
        if not any(r["name"] == ref for r in resolved):
            resolved.append({"name": ref, "oid": resolve_ref(repo, ref)})
    if not resolved:
        raise TrueUpError("no commits to discover: repository has no resolvable refs")
    unique = {}
    for item in resolved:
        unique.setdefault((item["name"], item["oid"]), item)
    return sorted(unique.values(), key=lambda r: (r["name"], r["oid"])), skipped


def shallow_info(repo):
    shallow = repo.run("rev-parse", "--is-shallow-repository", ok=(0, 128)).decode().strip() == "true"
    boundary = []
    if shallow:
        file = repo.common_dir / "shallow"
        if file.exists():
            boundary = sorted(line.strip() for line in file.read_text().splitlines() if line.strip())
    partial = bool(repo.run("config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$", ok=(0, 1)).strip())
    return shallow, boundary, partial


def enumerate_reachable(repo, oids):
    """Return ({sha: {'parents': [...], 'ts': int}}, missing_oids)."""
    raw = repo.run("rev-list", "--parents", "--timestamp", "--missing=print", *oids, timeout=1800).decode()
    commits, missing = {}, []
    for line in raw.splitlines():
        if line.startswith("?"):
            missing.append(line[1:])
            continue
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        commits[parts[1]] = {"parents": parts[2:], "ts": int(parts[0])}
    return commits, sorted(set(missing))


def topological_order(commits):
    """Parent-before-child order. Among commits whose parents are all emitted, the smallest
    (committer timestamp, object id) goes first. Timestamps only break ties between causally
    unordered commits; correctness of the order never depends on them."""
    pending = {}
    children = {}
    for sha, info in commits.items():
        inside = [p for p in info["parents"] if p in commits]
        pending[sha] = len(set(inside))
        for parent in set(inside):
            children.setdefault(parent, []).append(sha)
    heap = [(commits[s]["ts"], s) for s, n in pending.items() if n == 0]
    heapq.heapify(heap)
    order = []
    while heap:
        _, sha = heapq.heappop(heap)
        order.append(sha)
        for child in children.get(sha, ()):
            pending[child] -= 1
            if pending[child] == 0:
                heapq.heappush(heap, (commits[child]["ts"], child))
    if len(order) != len(commits):
        raise TrueUpError("commit graph contains a cycle or inconsistent parents; cannot order history")
    return order


def build_scope(repo, refs=None, all_local=False, include_remotes=False):
    resolved, skipped = resolve_scope(repo, refs, all_local, include_remotes)
    commits, missing = enumerate_reachable(repo, sorted({r["oid"] for r in resolved}))
    shallow, boundary, partial = shallow_info(repo)
    boundary_in_scope = sorted(b for b in boundary if b in commits)
    order = topological_order(commits)
    notes = []
    if shallow:
        notes.append(f"shallow repository: {len(boundary_in_scope)} commit(s) are shallow boundaries whose ancestry is unavailable; "
                     "history before them was NOT examined")
    if missing:
        notes.append(f"{len(missing)} referenced object(s) are missing from the object database; their history was NOT examined")
    if partial:
        notes.append("partial clone: blobs may be absent locally; affected commits will be recorded as partially inspected "
                     "(lazy fetching is disabled)")
    return {
        "scope": {"refs": resolved, "all_local": bool(all_local), "include_remotes": bool(include_remotes)},
        "coverage": {"shallow": shallow, "shallow_boundary": boundary_in_scope, "missing_objects": missing,
                     "partial_clone": partial, "skipped_refs": skipped, "complete": not (shallow or missing),
                     "notes": notes},
        "ordering": "parent-before-child topological order; among ready commits the smallest "
                    "(committer timestamp, object id) is emitted first (timestamps only break ties)",
        "commits": [{"sha": s, "parents": commits[s]["parents"], "ts": commits[s]["ts"]} for s in order],
    }


# ---------------------------------------------------------------------------------------------
# Per-commit inspection


def parse_commit_object(raw):
    head, _, body = raw.partition(b"\n\n")
    info = {"tree": None, "parents": [], "author": None, "committer": None, "encoding": "utf-8"}
    for line in head.split(b"\n"):
        if line.startswith(b" "):
            continue  # continuation of a multi-line header such as gpgsig
        key, _, value = line.partition(b" ")
        text = value.decode("utf-8", "replace")
        if key == b"tree":
            info["tree"] = text
        elif key == b"parent":
            info["parents"].append(text)
        elif key in (b"author", b"committer"):
            info[key.decode()] = text
        elif key == b"encoding":
            info["encoding"] = text
    try:
        message = body.decode(info["encoding"], "replace")
    except LookupError:
        message = body.decode("utf-8", "replace")
    info["message"] = message
    return info


def split_identity(value):
    match = re.match(r"^(.*?)\s*<([^>]*)>\s+(\d+)\s+([+-]\d{4})$", value or "")
    if not match:
        return {"name": value or "", "email": None, "timestamp": None, "tz": None}
    return {"name": match.group(1), "email": match.group(2), "timestamp": int(match.group(3)), "tz": match.group(4)}


def parse_trailers(message):
    paragraphs = [p for p in message.rstrip().split("\n\n")]
    if len(paragraphs) < 2:
        return []
    last = paragraphs[-1].split("\n")
    trailers, current = [], None
    for line in last:
        if re.match(r"^[A-Za-z][A-Za-z0-9-]*:\s", line):
            key, _, value = line.partition(":")
            current = {"key": key, "value": value.strip()}
            trailers.append(current)
        elif line.startswith((" ", "\t")) and current:
            current["value"] += " " + line.strip()
        else:
            return []
    return trailers


def parse_raw_diff(data):
    """Parse `diff-tree -z --raw`. Returns a list of change dicts with exact (byte) paths."""
    # Only delimiter-terminated fields are complete, including at the byte cap.
    parts = data.split(b"\0")[:-1]
    entries, i = [], 0
    while i < len(parts):
        head = parts[i]
        if not head.startswith(b":"):
            i += 1
            continue
        fields = head[1:].decode("ascii", "replace").split()
        if len(fields) < 5:
            i += 1
            continue
        status = fields[4]
        kind, score = status[0], (int(status[1:]) if status[1:].isdigit() else None)
        i += 1
        if i >= len(parts):
            break
        first = parts[i]
        i += 1
        second = None
        if kind in "RC":
            if i >= len(parts):
                break
            second = parts[i]
            i += 1
        new_raw = second if second is not None else first
        old_raw = first if second is not None else None
        text, lossless = path_text(new_raw)
        entry = {"path": text, "path_lossless": lossless, "status": kind, "score": score,
                 "old_mode": fields[0], "new_mode": fields[1], "old_oid": fields[2], "new_oid": fields[3],
                 "old_path": path_text(old_raw)[0] if old_raw is not None else None,
                 "symlink": "120000" in (fields[0], fields[1]), "submodule": "160000" in (fields[0], fields[1])}
        entries.append(entry)
    return entries


def split_patch_sections(patch_bytes):
    """Split a unified multi-file patch at `diff --git ` header lines (always at column 0)."""
    sections, start = [], None
    position = 0
    for line in patch_bytes.split(b"\n"):
        if line.startswith(b"diff --git "):
            if start is not None:
                sections.append(patch_bytes[start:position])
            start = position
        position += len(line) + 1
    if start is not None:
        sections.append(patch_bytes[start:min(position, len(patch_bytes))])
    return sections


DIFF_ARGS = ["--no-ext-diff", "--no-textconv", "--no-color", "--submodule=short", "-M"]


def inspect_commit(repo, sha, limits):
    """Collect metadata and a bounded, path-attributed diff for one commit.

    Merge commits are compared with their first parent for the path list (what the merge brought
    into the line of history it joined) and with `--cc` for the combined diff (hunks that differ
    from every parent, i.e. conflict resolutions and evil merges). Work on the merged-in side
    branch is not re-analysed here: each of those commits is in the manifest and analysed
    individually.
    """
    limitations = []
    try:
        raw = repo.run("cat-file", "commit", sha)
    except GitFailure as exc:
        raise GitFailure(f"commit object {sha} unreadable: {exc}") from exc
    info = parse_commit_object(raw)
    parents = info["parents"]
    message = info["message"]
    message_truncated = len(message) > limits["max_message_chars"]
    if message_truncated:
        message = message[:limits["max_message_chars"]]
        limitations.append(f"commit message truncated to {limits['max_message_chars']} characters")
    kind = "root" if not parents else ("merge" if len(parents) > 1 else "normal")
    author, committer = split_identity(info["author"]), split_identity(info["committer"])
    result = {
        "sha": sha, "tree": info["tree"], "parents": parents, "kind": kind,
        "author": author, "committer": committer, "message": message, "message_truncated": message_truncated,
        "trailers": parse_trailers(info["message"]), "files": [], "files_total": 0, "limitations": limitations,
        "merge": None, "combined_diff": None, "diff_truncated": False, "empty": False,
    }
    cap_raw = limits["max_raw_bytes"]
    if parents:
        trees = [parents[0], sha]
        raw_args = ["diff-tree", "-r", "-z", "--raw", *DIFF_ARGS, *trees]
        patch_args = ["diff-tree", "-r", "-p", f"-U{limits['context_lines']}", *DIFF_ARGS, *trees]
    else:
        raw_args = ["diff-tree", "--root", "--no-commit-id", "-r", "-z", "--raw", *DIFF_ARGS, sha]
        patch_args = ["diff-tree", "--root", "--no-commit-id", "-r", "-p", f"-U{limits['context_lines']}", *DIFF_ARGS, sha]
    raw_diff, raw_truncated = repo.run_capped(*raw_args, cap=cap_raw)
    entries = parse_raw_diff(raw_diff)
    if raw_truncated:
        limitations.append("changed-path list truncated at the raw diff byte limit")
        result["diff_truncated"] = True
    result["files_total"] = len(entries)
    result["empty"] = not entries and kind != "merge"
    patch, patch_truncated = repo.run_capped(*patch_args, cap=limits["max_diff_bytes"]) if entries else (b"", False)
    sections = split_patch_sections(patch)
    attributed = len(sections) >= len(entries) if not patch_truncated else True
    if entries and not patch_truncated and len(sections) != len(entries):
        attributed = False
        limitations.append("patch sections could not be matched to the changed-path list; diff text is unattributed")
    if patch_truncated:
        limitations.append(f"diff truncated at {limits['max_diff_bytes']} bytes; later files were not shown to analysis")
        result["diff_truncated"] = True
    for index, entry in enumerate(entries):
        section = sections[index] if attributed and index < len(sections) else None
        entry["patch"] = None
        entry["binary"] = False
        if section is not None:
            if re.search(rb"^(Binary files .* differ|GIT binary patch)", section, re.M):
                entry["binary"] = True
            else:
                entry["patch"] = section.decode("utf-8", "backslashreplace")
        entry["patch_available"] = entry["patch"] is not None
        if section is None and not entry["symlink"] and not entry["submodule"]:
            entry["not_shown"] = True
        if entry["new_oid"].strip("0") and not entry["submodule"]:
            entry["blob_oid"] = entry["new_oid"]
    if not attributed and patch:
        result["unattributed_patch"] = patch.decode("utf-8", "backslashreplace")
    result["files"] = entries
    if len(parents) > 1:
        merged, _ = repo.run_capped("rev-list", "--count", *parents[1:], "--not", parents[0], cap=64)
        side = repo.run("log", "-n", str(limits["max_merged_listed"]), "--format=%H%x00%s", *parents[1:], "--not", parents[0]).decode("utf-8", "replace")
        listed = [line.split("\x00", 1) for line in side.splitlines() if line]
        result["merge"] = {"first_parent": parents[0], "other_parents": parents[1:],
                           "merged_commit_count": int(merged.decode().strip() or 0),
                           "merged_commits": [{"sha": a, "subject": b} for a, b in listed]}
        combined, c_truncated = repo.run_capped("diff-tree", "--cc", "--no-commit-id", "-r", "-p", "-U3", *DIFF_ARGS[:-1], sha,
                                                cap=limits["max_combined_bytes"])
        result["combined_diff"] = combined.decode("utf-8", "backslashreplace") or None
        if c_truncated:
            limitations.append("combined (conflict-resolution) diff truncated")
    return result


def file_at(repo, oid, max_bytes):
    """Return (text, truncated) for a blob, or (None, reason)."""
    try:
        size = int(repo.run("cat-file", "-s", oid).decode().strip())
    except (GitFailure, ValueError):
        return None, "blob unavailable"
    data, truncated = repo.run_capped("cat-file", "blob", oid, cap=max_bytes)
    if b"\0" in data[:8192]:
        return None, "binary"
    return safe_text(data), truncated or size > max_bytes


def reverted_commits(message):
    return re.findall(r"This reverts commit ([0-9a-f]{40}(?:[0-9a-f]{24})?)", message or "")


def region_commits(repo, revision, path, line, timeout=60, limit=300):
    """Commits whose changes touched the line (git's own line-level history). Empty on failure."""
    try:
        out = repo.run("log", f"-n{limit}", "--format=%H", "-s", "-L", f"{line},{line}:{path}", revision, ok=(0,), timeout=timeout)
    except TrueUpError:
        return None
    return [l for l in out.decode().split() if HEX.match(l)]


def path_history_names(repo, revision, path, timeout=60):
    """Earlier names of `path` according to Git's rename detection (a heuristic, never identity)."""
    try:
        out = repo.run("log", "--follow", "-M", "--name-status", "--format=%H", revision, "--", path, timeout=timeout).decode("utf-8", "replace")
    except TrueUpError:
        return None
    names = [{"path": path, "commit": None, "similarity": None}]
    commit = None
    for line in out.splitlines():
        if HEX.match(line.strip() or "x"):
            commit = line.strip()
        elif line.startswith("R") and "\t" in line:
            fields = line.split("\t")
            if len(fields) >= 3:
                names.append({"path": fields[1], "commit": commit, "similarity": int(fields[0][1:] or 0)})
    return names


def map_paths(repo, old_rev, new_rev, timeout=120):
    """Path status between two snapshots by Git rename detection: {old_path: (status, new_path, score)}."""
    data, truncated = repo.run_capped("diff-tree", "-r", "-z", "--raw", *DIFF_ARGS, old_rev, new_rev, cap=64 * 1024 * 1024, timeout=timeout)
    mapping = {}
    for e in parse_raw_diff(data):
        old = e["old_path"] or e["path"]
        mapping[old] = (e["status"], e["path"], e["score"])
    return mapping, truncated


def blame_lines(repo, revision, path, timeout=120):
    """line number -> commit for `path` at revision (None revision = working tree)."""
    args = ["blame", "--no-textconv", "--porcelain", "-w"]
    if revision:
        args += [revision]
    args += ["--", path]
    try:
        out = repo.run(*args, timeout=timeout).decode("utf-8", "replace")
    except TrueUpError:
        return None
    result = {}
    for line in out.splitlines():
        match = re.match(r"^([0-9a-f]{40}(?:[0-9a-f]{24})?) \d+ (\d+)(?: \d+)?$", line)
        if match:
            result[int(match.group(2))] = match.group(1)
    return result
