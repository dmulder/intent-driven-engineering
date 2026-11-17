"""Canonical storage, exact Git snapshots, and structural transition validation."""
import base64
from contextlib import contextmanager
import difflib
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess

from .schema import SCHEMAS, check, published


class IntentError(Exception):
    pass


class ValidationError(IntentError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("structural validation failed")


def canonical(value):
    # Floats, NaN, and non-JSON Python objects are deliberately not canonicalizable.
    def inspect(v):
        if type(v) in (str, int, bool, type(None)):
            return
        if isinstance(v, list):
            for child in v:
                inspect(child)
            return
        if isinstance(v, dict) and all(isinstance(k, str) for k in v):
            for child in v.values():
                inspect(child)
            return
        raise IntentError("canonical JSON permits only objects, arrays, strings, integers, booleans, null")
    inspect(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except UnicodeError as exc:
        raise IntentError("canonical JSON must contain valid Unicode") from exc


def digest(data):
    return hashlib.sha256(data).hexdigest()


def seal(record):
    return {**record, "content_hash": digest(canonical({k: v for k, v in record.items() if k != "content_hash"}))}


def loads(data):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise IntentError(f"duplicate JSON key: {key}")
            out[key] = value
        return out
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_float=lambda _: (_ for _ in ()).throw(IntentError("floats forbidden")),
                          parse_constant=lambda _: (_ for _ in ()).throw(IntentError("nonfinite numbers forbidden")))
    except (ValueError, UnicodeError) as exc:
        raise IntentError(f"invalid JSON: {exc}") from exc


def write_json(path, value, immutable=False):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise IntentError(f"refusing metadata write through symlink: {path}")
    data = canonical(value) + b"\n"
    if path.exists() and immutable:
        if path.read_bytes() != data:
            raise IntentError(f"immutable record collision: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    descriptor, temporary = tempfile.mkstemp(prefix=".intentctl-", dir=path.parent)
    temp = Path(temporary)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    os.replace(temp, path)


def git(root, *args, input=None, ok=(0,)):
    result = subprocess.run(["git", "-C", str(root), *args], input=input,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode not in ok:
        raise IntentError(f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def root_path():
    return Path(os.fsdecode(git(Path.cwd(), "rev-parse", "--show-toplevel")[:-1]))


@contextmanager
def writer_lock(root):
    """Serialize metadata writers per worktree; fail fast instead of losing a draft or manifest update."""
    try:
        import fcntl
    except ImportError as exc:
        raise IntentError("metadata writers need POSIX file locking (fcntl); unsupported on this platform") from exc
    raw = git(root, "rev-parse", "--git-path", "intentctl.lock").decode().strip()
    path = Path(raw) if Path(raw).is_absolute() else root / raw
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise IntentError(f"cannot open writer lock {path}: {exc.strerror}") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise IntentError(f"another intentctl writer holds {path}; wait for it to finish, then retry") from exc
        yield
    finally:
        os.close(descriptor)


def head(root):
    value = git(root, "rev-parse", "--verify", "HEAD", ok=(0, 128)).decode().strip()
    return value or None


def safe_path(path):
    return (isinstance(path, str) and bool(path) and "\\" not in path and "\x00" not in path
            and not path.startswith("/") and all(p not in ("", ".", "..", ".git") for p in path.split("/")))


def metadata(path):
    return path.startswith(".intent/")


def snapshot(root, revision=None, staged=False, working=False):
    """Return path -> (Git mode, exact bytes); never follow a tracked symlink."""
    entries = {}
    if revision:
        raw = git(root, "ls-tree", "-rz", "--full-tree", revision)
        rows = raw.split(b"\0")
        for row in rows:
            if not row:
                continue
            header, path = row.split(b"\t", 1)
            mode, kind, oid = header.decode().split()
            if kind != "blob":
                raise IntentError("submodules are unsupported; remove them from this prototype's repository")
            try:
                name = path.decode("utf-8")
            except UnicodeError as exc:
                raise IntentError("non-UTF-8 filenames are unsupported") from exc
            entries[name] = (mode, git(root, "cat-file", "blob", oid))
    else:
        raw = git(root, "ls-files", "--stage", "-z")
        for row in raw.split(b"\0"):
            if not row:
                continue
            header, path = row.split(b"\t", 1)
            mode, oid, stage = header.decode().split()
            if stage != "0":
                raise IntentError("unmerged index: resolve source and metadata conflicts before validation")
            if mode == "160000":
                raise IntentError("submodules are unsupported")
            try:
                name = path.decode("utf-8")
            except UnicodeError as exc:
                raise IntentError("non-UTF-8 filenames are unsupported") from exc
            entries[name] = (mode, git(root, "cat-file", "blob", oid))
        if working:
            for path in list(entries):
                file = root / path
                if file.is_symlink():
                    entries[path] = ("120000", os.fsencode(os.readlink(file)))
                elif file.is_file():
                    entries[path] = ("100755" if file.stat().st_mode & 0o111 else "100644", file.read_bytes())
                else:
                    del entries[path]
            intent = root / ".intent"
            if intent.exists():
                for file in intent.rglob("*"):
                    name = file.relative_to(root).as_posix()
                    if file.is_file() and not file.is_symlink() and not any(p in ("drafts", "cache") for p in file.relative_to(intent).parts):
                        entries[name] = ("100644", file.read_bytes())
    return entries


def source(entries):
    return {p: v for p, v in entries.items() if not metadata(p)}


def ref(entry):
    if entry is None:
        return None
    mode, data = entry
    return {"source": digest(data), "mode": mode}


def inventory(entries):
    return {p: ref(v) for p, v in sorted(source(entries).items())}


def object_record(data):
    return seal({"schema_version": 1, "id": digest(data), "encoding": "base64",
                 "data": base64.b64encode(data).decode("ascii")})


def put_object(root, data):
    record = object_record(data)
    write_json(root / ".intent/objects" / (record["id"] + ".json"), record, immutable=True)
    return record["id"]


@lru_cache(maxsize=16)
def opcodes(a, b):
    """SequenceMatcher is quadratic on bytes and many bindings share one file diff: compute each pair once."""
    return tuple(difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes())


def intervals(before, after):
    a = before[1] if before else b""
    b = after[1] if after else b""
    opaque = any(e and (e[0] == "120000" or b"\0" in e[1]) for e in (before, after))
    if opaque or a == b:
        return [{"before": [0, len(a)], "after": [0, len(b)]}]
    # SequenceMatcher is deterministic; textual matching does not establish identity.
    return [{"before": [i, j], "after": [k, l]}
            for tag, i, j, k, l in opcodes(a, b)
            if tag != "equal"]


def changes_between(before, after):
    return [{"path": path, "before": ref(before.get(path)), "after": ref(after.get(path)),
             "intervals": intervals(before.get(path), after.get(path))}
            for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]


def touches(start, end, other_start, other_end):
    if start == end or other_start == other_end:
        return start <= other_end and other_start <= end
    return start < other_end and other_start < end


def map_preserved_range(start, end, before, after):
    """Map [start,end) through a file change when it lies entirely in equal bytes.

    Returns (new_start, new_end) or None when deletion, mode/opaque change, or any
    overlap with a non-equal region makes relocation authoritative rather than
    coordinate maintenance. Textual equality never invents semantic identity.
    """
    if before is None or after is None:
        return None
    if before[0] != after[0]:
        return None
    a, b = before[1], after[1]
    if not (0 <= start <= end <= len(a)):
        return None
    if before[0] == "120000" or b"\0" in a or b"\0" in b:
        return (start, end) if a == b and (start, end) == (0, len(a)) else None
    if a == b:
        return (start, end)
    mapped = []
    cursor = start
    for tag, i1, i2, j1, j2 in opcodes(a, b):
        if cursor >= end and not (start == end and cursor == start):
            break
        if tag != "equal":
            if touches(start, end, i1, i2):
                return None
            continue
        if start == end:
            if i1 <= start <= i2:
                return (j1 + (start - i1), j1 + (start - i1))
            continue
        overlap_start = max(cursor, i1)
        overlap_end = min(end, i2)
        if overlap_start >= overlap_end:
            continue
        if overlap_start != cursor:
            return None
        mapped.append((j1 + (overlap_start - i1), j1 + (overlap_end - i1)))
        cursor = overlap_end
    if start == end:
        return None
    if cursor != end or not mapped:
        return None
    new_start, new_end = mapped[0][0], mapped[-1][1]
    if a[start:end] != b[new_start:new_end]:
        return None
    return (new_start, new_end)


def live_target(binding, current_source, objects):
    """Resolve a binding onto current bytes via exact pin or preserved-range mapping."""
    target = binding["target"]
    entry = current_source.get(target["path"])
    if entry is None:
        return None
    pinned = objects.get(target["source"])
    if pinned is None or not (0 <= target["start"] <= target["end"] <= len(pinned)):
        return None
    expected = {"source": target["source"], "mode": target["mode"]}
    if ref(entry) == expected:
        return {"path": target["path"], "start": target["start"], "end": target["end"],
                "mode": target["mode"], "content": pinned[target["start"]:target["end"]]}
    mapped = map_preserved_range(target["start"], target["end"], (target["mode"], pinned), entry)
    if mapped is None:
        return None
    start, end = mapped
    content = pinned[target["start"]:target["end"]]
    if entry[1][start:end] != content:
        return None
    return {"path": target["path"], "start": start, "end": end, "mode": entry[0], "content": content}


def range_affected(path, start, end, change):
    return (path == change["path"] and
            (change["before"] is None or change["after"] is None or
             change["before"]["mode"] != change["after"]["mode"] or
             any(touches(start, end, *part["before"]) for part in change["intervals"])))


def binding_affected(binding, change, live=None):
    if live is not None:
        return range_affected(live["path"], live["start"], live["end"], change)
    target = binding["target"]
    return range_affected(target["path"], target["start"], target["end"], change)


def affected_by_changes(bindings, changes, current_source=None, objects=None):
    """Return binding id -> binding for ranges that touch a change interval."""
    affected = {}
    for i, binding in bindings.items():
        if binding["status"] not in ("active", "unresolved"):
            continue
        live = None
        if current_source is not None and objects is not None:
            live = live_target(binding, current_source, objects)
            if live is None:
                # Unresolvable pins are treated as affected so agents must rebind.
                if any(binding["target"]["path"] == change["path"] for change in changes):
                    affected[i] = binding
                continue
        for change in changes:
            if binding_affected(binding, change, live=live):
                affected[i] = binding
                break
    return affected


VERIFICATION_STATUSES = ("declared", "pending", "verified", "failed", "unverifiable")


def verification_of(binding):
    value = binding.get("verification") or {"status": "declared"}
    return value


def record_path(kind, identity, revision=None):
    if kind in ("intent", "binding"):
        return f".intent/{kind}s/{identity}/{revision:06d}.json"
    return f".intent/{kind}s/{identity}.json"


def identity(config, kind, key):
    return {"intent": "I", "binding": "B", "event": "E"}[kind] + "-" + digest(canonical([config["namespace"], kind, key]))


EMPTY_MANIFEST = {"schema_version": 1, "intents": {}, "bindings": {}, "events": []}


class Store:
    def __init__(self, entries, enforce_schema_definitions=True):
        self.entries = entries
        self.records = {}
        self.errors = []
        self.objects = {}
        self.intents = {}
        self.bindings = {}
        self.events = {}
        self.manifest = EMPTY_MANIFEST
        self.config = None
        self.baseline = None
        for path, (mode, data) in sorted(entries.items()):
            if not metadata(path):
                continue
            parts = PurePosixPath(path).parts
            if len(parts) > 1 and parts[1] in ("drafts", "cache"):
                self.error("TRACKED_DRAFT", path, "drafts/cache must never be committed")
                continue
            if mode == "120000":
                self.error("METADATA_SYMLINK", path, "canonical metadata cannot be a symlink")
                continue
            if len(parts) > 1 and parts[1] == "schema":
                if path not in {f".intent/schema/{name}.schema.json" for name in SCHEMAS}:
                    self.error("UNKNOWN_RECORD", path, "unknown schema file")
                continue
            if not path.endswith(".json"):
                if path not in (".intent/README.md", ".intent/.gitignore"):
                    self.error("UNKNOWN_RECORD", path, "only defined canonical records and metadata documentation are allowed")
                continue
            try:
                value = loads(data)
                kind = self.kind(path)
                if kind is None:
                    self.error("UNKNOWN_RECORD", path, "unknown canonical JSON location")
                    continue
                problems = check(value, SCHEMAS[kind])
                if problems:
                    for problem in problems:
                        self.error("SCHEMA", path, problem)
                    continue
                if canonical(value) + b"\n" != data:
                    self.error("CANONICAL", path, "record must use canonical JSON plus one newline")
                if kind not in ("config", "manifest") and value != seal(value):
                    self.error("HASH", path, "content_hash mismatch")
                self.records[path] = value
                if kind == "manifest":
                    self.manifest = value
                elif kind == "config":
                    self.config = value
                elif kind == "baseline":
                    self.baseline = value
                elif kind == "object":
                    if path != record_path("object", value["id"]):
                        self.error("PATH", path, "object identity does not match path")
                    raw = base64.b64decode(value["data"], validate=True)
                    if digest(raw) != value["id"] or base64.b64encode(raw).decode() != value["data"]:
                        self.error("OBJECT_HASH", path, "source snapshot hash/encoding mismatch")
                    self.objects[value["id"]] = raw
                else:
                    expected = record_path(kind, value["id"], value.get("revision"))
                    prefix = {"intent": "I-", "binding": "B-", "event": "E-"}[kind]
                    if path != expected or not value["id"].startswith(prefix):
                        self.error("PATH", path, "record identity/revision does not match path")
                    collection = getattr(self, kind + "s")
                    key = (value["id"], value["revision"]) if kind != "event" else value["id"]
                    collection[key] = value
            except (IntentError, ValueError, TypeError) as exc:
                self.error("PARSE", path, str(exc))
        if enforce_schema_definitions:
            for name in SCHEMAS:
                path = f".intent/schema/{name}.schema.json"
                entry = entries.get(path)
                if entry is None or entry[1] != canonical(published(name)) + b"\n":
                    self.error("SCHEMA_DEFINITION", path, "published schema must match this validator version; run init for missing schemas")
        if self.config is None:
            self.error("MISSING_CONFIG", ".intent/config.json", "run init")
        if ".intent/manifests/current.json" not in self.records:
            self.error("MISSING_MANIFEST", ".intent/manifests/current.json", "run init")
        if self.baseline is None:
            self.error("MISSING_BASELINE", ".intent/baseline.json", "run baseline")

    @staticmethod
    def kind(path):
        if path == ".intent/config.json":
            return "config"
        if path == ".intent/baseline.json":
            return "baseline"
        if path == ".intent/manifests/current.json":
            return "manifest"
        for plural in ("intents", "bindings", "events", "objects"):
            if path.startswith(f".intent/{plural}/"):
                return plural[:-1]
        return None

    def error(self, code, path, message, **details):
        self.errors.append({"code": code, "path": path, "message": message, **details})

    def current(self, kind):
        collection = getattr(self, kind + "s")
        return {i: collection[(i, r)] for i, r in self.manifest[kind + "s"].items() if (i, r) in collection}

    def entry(self, value):
        if value is None:
            return None
        if value["source"] not in self.objects:
            raise IntentError(f"missing source snapshot {value['source']}")
        return value["mode"], self.objects[value["source"]]

    def integrity(self, final_source):
        for path, value in self.baseline["files"].items():
            if not safe_path(path) or metadata(path) or value["source"] not in self.objects:
                self.error("BASELINE_REFERENCE", path, "invalid baseline path or missing snapshot")
        for kind in ("intent", "binding"):
            collection = getattr(self, kind + "s")
            grouped = {}
            for (i, r), value in sorted(collection.items()):
                grouped.setdefault(i, []).append(r)
                previous = collection.get((i, r - 1))
                expected = previous["content_hash"] if previous else None
                if (r > 1 and previous is None) or value["previous_hash"] != expected:
                    self.error("REVISION_CHAIN", i, f"revision {r}: missing/mismatched predecessor")
                event = self.events.get(value["created_by_event"])
                ref_value = {"type": kind, "id": i, "revision": r}
                if event is None or ref_value not in event["records"]:
                    self.error("ORIGIN", i, f"revision {r}: originating event missing record reference")
            for i, revisions in grouped.items():
                if self.manifest[kind + "s"].get(i) != max(revisions):
                    self.error("HEAD", i, "manifest must identify latest revision, not omit history")
            for i, r in self.manifest[kind + "s"].items():
                if (i, r) not in collection:
                    self.error("REFERENCE", i, f"missing {kind} revision {r}")
        for (i, r), intent in sorted(self.intents.items()):
            for related in intent["related_intents"]:
                if related not in self.manifest["intents"]:
                    self.error("REFERENCE", i, f"unknown related intent {related}")
        for (i, r), binding in sorted(self.bindings.items()):
            if (binding["intent_id"], binding["intent_revision"]) not in self.intents:
                self.error("REFERENCE", i, f"revision {r}: unknown intent revision")
            for previous in binding["predecessors"]:
                if previous not in self.manifest["bindings"]:
                    self.error("REFERENCE", i, f"unknown predecessor binding {previous}")
            target = binding["target"]
            if not safe_path(target["path"]) or metadata(target["path"]):
                self.error("TARGET", i, "target must be an ordinary repository-relative source path")
            raw = self.objects.get(target["source"])
            if raw is None or not (0 <= target["start"] <= target["end"] <= len(raw)):
                self.error("TARGET", i, f"revision {r}: missing snapshot or invalid byte range")
            elif (target["mode"] == "120000" or b"\0" in raw) and (target["start"], target["end"]) != (0, len(raw)):
                self.error("OPAQUE_RANGE", i, "binary/symlink targets require whole-object ranges")
        # Active bindings may keep an older snapshot pin when edits elsewhere leave their
        # bytes intact and uniquely mappable. Overlapping or ambiguous moves still need
        # explicit rebinding; textual equality never invents semantic identity.
        for i, binding in sorted(self.current("binding").items()):
            if binding["status"] not in ("active", "unresolved"):
                continue
            target = binding["target"]
            live = live_target(binding, final_source, self.objects)
            if live is None:
                self.error("STALE_TARGET", i, f"{target['path']}:{target['start']}:{target['end']}: explicitly rebind; range missing or not uniquely preserved")
            intent = self.current("intent").get(binding["intent_id"])
            if intent and intent["status"] != "active":
                self.error("LIFECYCLE", i, "retired/superseded intent cannot retain active bindings")
            status = verification_of(binding)["status"]
            if status == "verified":
                value = verification_of(binding)
                if not value.get("method") or not value.get("evidence"):
                    self.error("VERIFICATION", i, "verified bindings require method and evidence")
            elif status not in VERIFICATION_STATUSES:
                self.error("VERIFICATION", i, f"unknown verification status {status}")
        events = self.manifest["events"]
        if len(set(events)) != len(events) or set(events) != set(self.events):
            self.error("EVENT_INDEX", ".intent/manifests/current.json", "event list must contain every event exactly once")
        for event in self.events.values():
            for record in event["records"]:
                collection = getattr(self, record["type"] + "s")
                value = collection.get((record["id"], record["revision"]))
                if not value or value["created_by_event"] != event["id"]:
                    self.error("REFERENCE", event["id"], f"invalid owned record {record}")
            for change in event["changes"]:
                if not safe_path(change["path"]) or metadata(change["path"]):
                    self.error("EVENT_PATH", event["id"], "invalid source path")
                try:
                    before, after = self.entry(change["before"]), self.entry(change["after"])
                    if before == after or change["intervals"] != intervals(before, after):
                        self.error("COVERAGE", event["id"], f"{change['path']}: declared ranges must equal exact snapshot diff")
                except IntentError as exc:
                    self.error("SNAPSHOT", event["id"], str(exc))


def validate(entries, base_entries=None, *, enforce_schema_definitions=True):
    # Historical audits can separately report old schema pins. Record validation
    # still uses this engine's trusted SCHEMAS, never repository-supplied schemas.
    store = Store(entries, enforce_schema_definitions=enforce_schema_definitions)
    base_entries = base_entries or {}
    base_exists = ".intent/config.json" in base_entries
    # Base history may predate the running validator's published schemas; enforce
    # schema-definition pins only on the proposed tree being validated.
    base = Store(base_entries, enforce_schema_definitions=False) if base_exists else None
    if store.errors:
        return store.errors
    if base and base.errors:
        return [{"code": "INVALID_BASE", "path": ".intent", "message": "base metadata is invalid; inspect trusted history"}] + base.errors

    for path, value in sorted(base_entries.items()):
        if not metadata(path) or path.startswith(".intent/schema/") or not path.endswith(".json"):
            continue
        if path == ".intent/manifests/current.json":
            continue
        if entries.get(path) != value:
            store.error("IMMUTABLE", path, "committed canonical history/config cannot be changed or deleted")
    store.integrity(source(entries))
    if any(e["code"] not in ("STALE_TARGET", "LIFECYCLE") for e in store.errors):
        return store.errors
    if base is None:
        if store.baseline["files"] != inventory(entries):
            store.error("BASELINE", ".intent/baseline.json", "first import must exactly inventory staged source; run baseline after staging source")
        # No fictitious pre-import event history is permitted.
        if store.events or store.intents or store.bindings:
            store.error("BOOTSTRAP", ".intent", "initial import must be an unknown baseline only; introduce intent in a subsequent transaction")
        return store.errors
    old_events = base.manifest["events"]
    if store.manifest["events"][:len(old_events)] != old_events:
        store.error("EVENT_ORDER", ".intent/manifests/current.json", "append events; do not reorder committed history")
        return store.errors
    current_source = source(base_entries).copy()
    current_intents = base.current("intent").copy()
    current_bindings = base.current("binding").copy()
    live = {}
    for i, binding in current_bindings.items():
        if binding["status"] in ("active", "unresolved"):
            resolved = live_target(binding, current_source, store.objects)
            if resolved is not None:
                live[i] = resolved
    new_events = store.manifest["events"][len(old_events):]

    def coverage_ranges(path, bindings_map, locations):
        ranges = []
        for i, binding in bindings_map.items():
            if binding["status"] not in ("active", "unresolved"):
                continue
            if i in locations:
                loc = locations[i]
                if loc["path"] == path:
                    ranges.append((loc["start"], loc["end"]))
            elif binding["target"]["path"] == path:
                ranges.append((binding["target"]["start"], binding["target"]["end"]))
        return sorted(ranges)

    def range_covered(a, z, ranges):
        cursor = a
        point_covered = False
        for start, end in ranges:
            if start <= cursor <= end:
                point_covered = True
                cursor = max(cursor, end)
        return not (cursor < z or (a == z and not point_covered))

    for event_id in new_events:
        event = store.events[event_id]
        changes = event["changes"]
        paths = [c["path"] for c in changes]
        if len(paths) != len(set(paths)):
            store.error("EVENT_PATH", event_id, "duplicate changed path")
        # Only ranges that touch a changed interval require explicit transitions.
        # Unrelated bindings in the same file stay pinned; coordinates advance by
        # deterministic equal-region mapping when the edit is unambiguous.
        affected = affected_by_changes(current_bindings, changes, current_source, store.objects)
        before_live = {i: dict(loc) for i, loc in live.items()}
        revised_live = set()
        transitions = event["intent_transitions"]
        seen = set()
        for transition in transitions:
            i = transition["intent_id"]
            b = transition["binding_id"]
            action = transition["action"]
            if (i, b) in seen:
                store.error("TRANSITION", event_id, f"duplicate declaration for {i}/{b}")
            seen.add((i, b))
            old_intent = current_intents.get(i)
            owned_intents = [store.intents[(r["id"], r["revision"])] for r in event["records"] if r["type"] == "intent" and r["id"] == i]
            new_intent = owned_intents[-1] if owned_intents else old_intent
            if (transition["from_revision"] != (old_intent["revision"] if old_intent else None)
                    or not new_intent or transition["to_revision"] != new_intent["revision"]):
                store.error("TRANSITION", event_id, f"{i}/{b}: incorrect intent revision transition")
            if action == "introduce":
                if old_intent or not owned_intents or transition["from_revision"] is not None:
                    store.error("TRANSITION", event_id, f"{i}: introduce requires a new intent")
            elif not old_intent:
                store.error("TRANSITION", event_id, f"{i}: action requires existing intent")
            if action in ("preserve", "rebind", "unapply") and owned_intents:
                store.error("TRANSITION", event_id, f"{i}: {action} cannot change the intent itself")
            if action in ("modify", "retire", "supersede") and not owned_intents:
                store.error("TRANSITION", event_id, f"{i}: {action} requires a new intent revision")
            if action in ("retire", "supersede") and new_intent and new_intent["status"] != {"retire": "retired", "supersede": "superseded"}[action]:
                store.error("TRANSITION", event_id, f"{i}: lifecycle does not match action")
            old_binding = current_bindings.get(b)
            if b and old_binding and old_binding["intent_id"] != i:
                store.error("TRANSITION", event_id, f"{b}: intent mismatch")
            declared = transition["binding_revisions"]
            owned_bindings = [store.bindings[(r["id"], r["revision"])] for r in event["records"] if r["type"] == "binding" and r["id"] == b]
            if declared != [v["revision"] for v in owned_bindings]:
                store.error("TRANSITION", event_id, f"{b}: declared binding revisions do not match event records")
            if b and not old_binding and not owned_bindings:
                store.error("REFERENCE", event_id, f"unknown binding {b}")
            if old_binding and old_binding["status"] == "unresolved" and b in affected and owned_bindings and owned_bindings[-1]["status"] == "unresolved":
                store.error("UNRESOLVED", event_id, f"{b}: resolve or end unresolved applicability before further source edits")
            if owned_bindings and any(v["intent_id"] != i for v in owned_bindings):
                store.error("TRANSITION", event_id, f"{b}: binding belongs to another intent")
            if b and action in ("unapply", "retire", "supersede", "rebind") and not owned_bindings:
                store.error("MISSING_BINDING_REVISION", event_id, f"{b}: {action} requires a binding revision even without source edits")
            if b in affected and not owned_bindings:
                store.error("MISSING_BINDING_REVISION", event_id, f"{b}: changed source requires explicit binding revision")
            if owned_bindings and action in ("unapply", "retire", "supersede") and owned_bindings[-1]["status"] not in ("ended", "historical"):
                store.error("TRANSITION", event_id, f"{b}: action requires ending current applicability")
            if owned_bindings and action in ("preserve", "rebind", "introduce") and owned_bindings[-1]["status"] not in ("active", "unresolved"):
                store.error("TRANSITION", event_id, f"{b}: action requires active or unresolved successor")
        for b, binding in affected.items():
            if (binding["intent_id"], b) not in seen:
                store.error("MISSING_TRANSITION", event_id,
                            f"{binding['target']['path']}:{binding['target']['start']}:{binding['target']['end']}: declare effect on {binding['intent_id']} binding {b}")
        for record in event["records"]:
            kind, i, r = record["type"], record["id"], record["revision"]
            current = current_intents if kind == "intent" else current_bindings
            prior = current.get(i)
            if r != (prior["revision"] + 1 if prior else 1):
                store.error("EVENT_REVISION", event_id, f"{i}: revisions must advance in event order")
            value = getattr(store, kind + "s")[(i, r)]
            if kind == "intent" and not any(t["intent_id"] == i for t in transitions):
                store.error("MISSING_TRANSITION", event_id, f"{i}: intent revision requires explicit action")
            if kind == "binding" and not any(t["binding_id"] == i for t in transitions):
                store.error("MISSING_TRANSITION", event_id, f"{i}: binding revision requires explicit action")
            current[i] = value
            if kind == "binding":
                revised_live.add(i)
                if value["status"] in ("active", "unresolved"):
                    # Owned successors define the assessed target for this event.
                    live[i] = {"path": value["target"]["path"], "start": value["target"]["start"],
                               "end": value["target"]["end"], "mode": value["target"]["mode"],
                               "content": store.objects[value["target"]["source"]][value["target"]["start"]:value["target"]["end"]]}
                else:
                    live.pop(i, None)
        for change in changes:
            path = change["path"]
            before_entry = current_source.get(path)
            if ref(before_entry) != change["before"]:
                store.error("SOURCE_CHAIN", event_id, f"{path}: before snapshot does not match preceding state")
            after_entry = None if change["after"] is None else store.entry(change["after"])
            before_ranges = coverage_ranges(path, affected if event["provenance"] == "known" else current_bindings, before_live)
            # Advance live coordinates for unaffected bindings; overlapping ranges already
            # required an explicit successor whose live entry was installed above.
            for b, loc in list(live.items()):
                if loc["path"] != path or b in affected or b in revised_live:
                    continue
                mapped = map_preserved_range(loc["start"], loc["end"], before_entry, after_entry)
                if mapped is None:
                    store.error("EVENT_TARGET", event_id, f"{b}: unaffected range could not be uniquely relocated; rebind explicitly")
                    live.pop(b, None)
                    continue
                live[b] = {**loc, "start": mapped[0], "end": mapped[1],
                           "mode": after_entry[0] if after_entry else loc["mode"]}
            if change["after"] is None:
                current_source.pop(path, None)
            else:
                current_source[path] = after_entry
            after_ranges = coverage_ranges(path, current_bindings, live)
            # Known provenance still requires both sides covered. Unknown may leave
            # unbound legacy before-bytes uncovered, but newly introduced after-bytes
            # always need at least one active binding so unknown cannot mint free code.
            for interval in change["intervals"]:
                if event["provenance"] == "known":
                    if change["before"] is not None:
                        a, z = interval["before"]
                        if not range_covered(a, z, before_ranges):
                            store.error("UNKNOWN_PROVENANCE", event_id, f"{path}: before [{a},{z}) lacks known intent; declare unknown provenance")
                    if change["after"] is not None:
                        a, z = interval["after"]
                        if not range_covered(a, z, after_ranges):
                            store.error("UNKNOWN_PROVENANCE", event_id, f"{path}: after [{a},{z}) lacks known intent; declare unknown provenance")
                else:
                    if change["after"] is None:
                        continue
                    # Mode-only transitions keep identical bytes; they do not introduce code.
                    if (change["before"] is not None and
                            change["before"]["source"] == change["after"]["source"]):
                        continue
                    a, z = interval["after"]
                    if a == z:
                        continue
                    if not range_covered(a, z, after_ranges):
                        store.error("NEW_CODE_REQUIRES_INTENT", event_id,
                                    f"{path}: after [{a},{z}) introduces unbound bytes; bind an intent (unknown provenance covers legacy before-bytes only)")
        for b, binding in current_bindings.items():
            if binding["status"] not in ("active", "unresolved"):
                continue
            loc = live.get(b) or live_target(binding, current_source, store.objects)
            if loc is None:
                store.error("EVENT_TARGET", event_id, f"{b}: active target must remain uniquely preserved or explicitly rebound at this event")
            else:
                live[b] = loc
            governing = current_intents.get(binding["intent_id"])
            if governing and governing["status"] != "active":
                store.error("EVENT_LIFECYCLE", event_id, f"{b}: end applicability when retiring/superseding intent")
    for change in changes_between(current_source, source(entries)):
        store.error("UNCOVERED_CHANGE", change["path"], "source differs from event chain; capture staged source and stage completed metadata", intervals=change["intervals"])
    return sorted(store.errors, key=lambda e: (e["code"], e["path"], e["message"]))
