"""Canonical storage, exact Git snapshots, and structural transition validation."""
import base64
import difflib
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


def intervals(before, after):
    a = before[1] if before else b""
    b = after[1] if after else b""
    opaque = any(e and (e[0] == "120000" or b"\0" in e[1]) for e in (before, after))
    if opaque or a == b:
        return [{"before": [0, len(a)], "after": [0, len(b)]}]
    # SequenceMatcher is deterministic; textual matching does not establish identity.
    return [{"before": [i, j], "after": [k, l]}
            for tag, i, j, k, l in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            if tag != "equal"]


def changes_between(before, after):
    return [{"path": path, "before": ref(before.get(path)), "after": ref(after.get(path)),
             "intervals": intervals(before.get(path), after.get(path))}
            for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]


def touches(start, end, other_start, other_end):
    if start == end or other_start == other_end:
        return start <= other_end and other_start <= end
    return start < other_end and other_start < end


def binding_affected(binding, change):
    target = binding["target"]
    return (target["path"] == change["path"] and
            (change["before"] is None or change["after"] is None or
             change["before"]["mode"] != change["after"]["mode"] or
             any(touches(target["start"], target["end"], *part["before"]) for part in change["intervals"])))


def record_path(kind, identity, revision=None):
    if kind in ("intent", "binding"):
        return f".intent/{kind}s/{identity}/{revision:06d}.json"
    return f".intent/{kind}s/{identity}.json"


def identity(config, kind, key):
    return {"intent": "I", "binding": "B", "event": "E"}[kind] + "-" + digest(canonical([config["namespace"], kind, key]))


EMPTY_MANIFEST = {"schema_version": 1, "intents": {}, "bindings": {}, "events": []}


class Store:
    def __init__(self, entries):
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
        # Every current active binding must target the exact final source. Moving a range
        # because of edits elsewhere is explicit rebinding, never an inferred identity.
        for i, binding in sorted(self.current("binding").items()):
            if binding["status"] not in ("active", "unresolved"):
                continue
            target = binding["target"]
            entry = final_source.get(target["path"])
            if ref(entry) != {"source": target["source"], "mode": target["mode"]}:
                self.error("STALE_TARGET", i, f"{target['path']}:{target['start']}:{target['end']}: explicitly rebind to current bytes")
            intent = self.current("intent").get(binding["intent_id"])
            if intent and intent["status"] != "active":
                self.error("LIFECYCLE", i, "retired/superseded intent cannot retain active bindings")
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


def validate(entries, base_entries=None):
    store = Store(entries)
    base_entries = base_entries or {}
    base_exists = ".intent/config.json" in base_entries
    base = Store(base_entries) if base_exists else None
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
    new_events = store.manifest["events"][len(old_events):]
    for event_id in new_events:
        event = store.events[event_id]
        changes = event["changes"]
        paths = [c["path"] for c in changes]
        if len(paths) != len(set(paths)):
            store.error("EVENT_PATH", event_id, "duplicate changed path")
        affected = {}
        # Any edit in a bound file requires explicit continuity for all its bindings:
        # whole-file snapshot identity changes even when byte intervals do not overlap.
        for i, binding in current_bindings.items():
            if binding["status"] in ("active", "unresolved") and binding["target"]["path"] in paths:
                affected[i] = binding
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
        for change in changes:
            path = change["path"]
            if ref(current_source.get(path)) != change["before"]:
                store.error("SOURCE_CHAIN", event_id, f"{path}: before snapshot does not match preceding state")
            if change["after"] is None:
                current_source.pop(path, None)
            else:
                current_source[path] = store.entry(change["after"])
            if event["provenance"] == "known":
                # Unknown is mandatory unless binding ranges cover ALL changed bytes.
                for interval in change["intervals"]:
                    for side, bindings in (("before", affected), ("after", current_bindings)):
                        if change[side] is None:
                            continue
                        a, z = interval[side]
                        ranges = sorted((b["target"]["start"], b["target"]["end"]) for b in bindings.values()
                                        if b["status"] in ("active", "unresolved") and b["target"]["path"] == path)
                        cursor = a
                        point_covered = False
                        for start, end in ranges:
                            if start <= cursor <= end:
                                point_covered = True
                                cursor = max(cursor, end)
                        if cursor < z or (a == z and not point_covered):
                            store.error("UNKNOWN_PROVENANCE", event_id, f"{path}: {side} [{a},{z}) lacks known intent; declare unknown provenance")
        for b, binding in current_bindings.items():
            if binding["status"] not in ("active", "unresolved"):
                continue
            target = binding["target"]
            if ref(current_source.get(target["path"])) != {"source": target["source"], "mode": target["mode"]}:
                store.error("EVENT_TARGET", event_id, f"{b}: active target must match source at this event, not only the final commit")
            governing = current_intents.get(binding["intent_id"])
            if governing and governing["status"] != "active":
                store.error("EVENT_LIFECYCLE", event_id, f"{b}: end applicability when retiring/superseding intent")
    for change in changes_between(current_source, source(entries)):
        store.error("UNCOVERED_CHANGE", change["path"], "source differs from event chain; capture staged source and stage completed metadata", intervals=change["intervals"])
    return sorted(store.errors, key=lambda e: (e["code"], e["path"], e["message"]))
