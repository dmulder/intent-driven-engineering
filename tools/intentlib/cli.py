"""Command-line workflows. Drafts are editable; finalization never stages files."""
import argparse
import copy
import os
import re
import shlex
import time
from pathlib import Path
import sys

from .core import (EMPTY_MANIFEST, IntentError, ValidationError, Store, affected_by_changes,
                   canonical, changes_between, digest, git, head, identity, inventory,
                   live_target, loads, metadata, put_object, record_path, ref, root_path,
                   safe_path, seal, snapshot, source, validate, verification_of, write_json)
from .schema import ACTIONS, SCHEMAS, check, published


def output(value, as_json=False):
    if as_json:
        print(canonical(value).decode())
    elif isinstance(value, dict) and "errors" in value and "valid" in value:
        print("valid" if value["valid"] else "invalid")
        for error in value["errors"]:
            print(f"{error['code']} {error['path']}: {error['message']}")
    else:
        import json
        print(json.dumps(value, indent=2, ensure_ascii=False))


def read_json(path):
    try:
        return loads(Path(path).read_bytes())
    except OSError as exc:
        raise IntentError(str(exc)) from exc


def config(root):
    return read_json(root / ".intent/config.json")


def manifest(root):
    return read_json(root / ".intent/manifests/current.json")


def event_id(root, key):
    if re.fullmatch(r"E-[0-9a-f]{64}", key):
        return key
    return identity(config(root), "event", key)


def draft_path(root, key):
    return root / ".intent/drafts" / (event_id(root, key) + ".json")


def load_draft(root, key):
    return read_json(draft_path(root, key))


def save_draft(root, key, draft):
    write_json(draft_path(root, key), draft)


def install_hooks(root, uninstall=False):
    raw = git(root, "rev-parse", "--git-path", "hooks").decode().strip()
    directory = Path(raw) if Path(raw).is_absolute() else root / raw
    directory.mkdir(parents=True, exist_ok=True)
    marker = "# intentctl managed dispatcher v1"
    engine = str(Path(__file__).resolve().parents[1] / "intentctl.py")
    runner = shlex.join([sys.executable, engine])
    missing = shlex.quote(f'intentctl is unavailable at {engine} (Python: {sys.executable}); reinstall at the same prefix, then run intentctl uninstall-hooks in this repository if removing it.')
    availability = (f'if [ ! -x {shlex.quote(sys.executable)} ] || [ ! -r {shlex.quote(engine)} ]; then\n'
                    f'  printf "%s\\n" {missing} >&2\n  exit 1\nfi\n')
    for name in ("pre-commit", "pre-push"):
        path = directory / name
        backup = directory / (name + ".intentctl-original")
        if path.is_symlink():
            raise IntentError(f"hook {path} is a symlink; install a dispatcher manually")
        managed = path.exists() and marker in path.read_text(errors="replace")
        if uninstall:
            if managed:
                path.unlink()
                if backup.exists():
                    backup.rename(path)
            continue
        if not managed:
            if backup.exists():
                raise IntentError(f"ambiguous hook backup {backup}; reconcile manually")
            if path.exists():
                path.rename(backup)
        # Resolve repository at execution time, including linked worktrees.
        # The original hook runs first and receives the same arguments/stdin.
        if name == "pre-commit":
            body = runner + " validate --staged\n"
            chain = 'if [ -x "$original" ]; then "$original" "$@" || exit $?; fi\n'
        else:
            # Preserve stdin for the original pre-push hook and our validator.
            body = runner + ' validate --push < "$input"\n'
            chain = ('input=$(mktemp) || exit 1\ntrap \'rm -f "$input"\' EXIT HUP INT TERM\ncat > "$input"\n'
                     'if [ -x "$original" ]; then "$original" "$@" < "$input" || exit $?; fi\n')
        text = ('#!/bin/sh\n' + marker + '\nroot=$(git rev-parse --show-toplevel) || exit 1\n'
                'original="$0.intentctl-original"\n' + chain + availability + body)
        if not path.exists() or path.read_text(errors="replace") != text:
            path.write_text(text)
        path.chmod(0o755)


def initialize(root, no_hooks=False):
    if not (root / ".intent/config.json").exists():
        entries = snapshot(root, staged=True)
        namespace = digest(canonical(inventory(entries)))
        write_json(root / ".intent/config.json", {"schema_version": 1, "namespace": namespace,
                   "coverage": "all-tracked-non-metadata"})
        write_json(root / ".intent/manifests/current.json", EMPTY_MANIFEST)
        make_baseline(root)
    else:
        cfg = config(root)
        if check(cfg, SCHEMAS["config"]):
            raise IntentError("existing config is invalid; run doctor; init will not replace it")
        if not (root / ".intent/manifests/current.json").exists() or not (root / ".intent/baseline.json").exists():
            raise IntentError("incomplete initialization: recover manifest/baseline from Git; init will not invent missing history")
    for name in SCHEMAS:
        file = root / ".intent/schema" / (name + ".schema.json")
        if not file.exists():
            write_json(file, published(name))
    for name in ("intents", "bindings", "events", "objects", "drafts"):
        (root / ".intent" / name).mkdir(exist_ok=True)
    ignore = root / ".intent/.gitignore"
    if not ignore.exists():
        ignore.write_text("drafts/\ncache/\n")
    if not no_hooks:
        install_hooks(root)
    return {"initialized": True, "hooks": not no_hooks, "baseline": "unknown provenance; not verified intent"}


def make_baseline(root):
    previous = head(root)
    old = snapshot(root, revision=previous) if previous else {}
    if ".intent/baseline.json" in old:
        raise IntentError("committed baseline is immutable; use events for subsequent changes")
    if manifest(root)["events"]:
        raise IntentError("cannot replace baseline after events exist")
    entries = snapshot(root, staged=True)
    for _, data in source(entries).values():
        put_object(root, data)
    baseline = seal({"schema_version": 1, "provenance": "unknown", "files": inventory(entries)})
    write_json(root / ".intent/baseline.json", baseline)
    return {"baseline_files": len(baseline["files"]), "provenance": "unknown"}


def pending_source(root, store=None):
    base_revision = head(root)
    base_entries = snapshot(root, revision=base_revision) if base_revision else {}
    state = source(base_entries).copy()
    store = store or Store(snapshot(root, working=True))
    if store.errors:
        raise IntentError(str(store.errors))
    old = Store(base_entries) if ".intent/config.json" in base_entries else None
    old_ids = old.manifest["events"] if old else []
    if store.manifest["events"][:len(old_ids)] != old_ids:
        raise IntentError("committed event order changed")
    for i in store.manifest["events"][len(old_ids):]:
        for change in store.events[i]["changes"]:
            if ref(state.get(change["path"])) != change["before"]:
                raise IntentError(f"broken pending source chain in {i}: {change['path']}")
            if change["after"] is None:
                state.pop(change["path"], None)
            else:
                state[change["path"]] = store.entry(change["after"])
    return state


def capture(root, args):
    store = Store(snapshot(root, working=True))
    before = pending_source(root, store)
    after = source(snapshot(root, staged=True))
    changes = changes_between(before, after)
    for change in changes:
        for state in (before, after):
            if change["path"] in state:
                put_object(root, state[change["path"]][1])
    event = {"schema_version": 1, "id": event_id(root, args.key), "kind": args.kind,
             "description": args.description, "rationale": args.rationale,
             "changes": changes, "intent_transitions": [], "provenance": args.provenance,
             "verification": [], "records": []}
    if (root / record_path("event", event["id"])).exists() or draft_path(root, args.key).exists():
        raise IntentError("event creation key already exists; choose a distinct --key")
    paths = {c["path"] for c in changes}
    current_bindings = store.current("binding")
    affected = affected_by_changes(current_bindings, changes, before, store.objects)
    required = []
    for b in affected.values():
        live = live_target(b, before, store.objects)
        required.append({"intent_id": b["intent_id"], "binding_id": b["id"],
                         "intent_revision": b["intent_revision"], "target": b["target"],
                         "live_target": ({"path": live["path"], "start": live["start"], "end": live["end"],
                                          "mode": live["mode"]} if live else None)})
    draft = {"event": event, "intents": [], "bindings": [], "required_declarations": required}
    save_draft(root, args.key, draft)
    return {"draft": str(draft_path(root, args.key).relative_to(root)),
            "changed_paths": sorted(paths), "required_declarations": required,
            "next": "complete provenance and intent_transitions; then finalize"}


def add_intent(root, args):
    draft = load_draft(root, args.event)
    current = manifest(root)
    if args.command == "intent-create":
        i = identity(config(root), "intent", args.key)
        if i in current["intents"]:
            raise IntentError("intent key already exists; use intent-revise")
        value = {"schema_version": 1, "id": i, "revision": 1, "previous_hash": None,
                 "created_by_event": draft["event"]["id"], "status": args.status, "kind": args.kind,
                 "title": args.title, "description": args.description, "rationale": args.rationale,
                 "requirements": args.requirement or [], "constraints": args.constraint or [],
                 "related_intents": args.related or []}
    else:
        i = args.id
        if i not in current["intents"]:
            raise IntentError(f"unknown intent {i}")
        old = read_json(root / record_path("intent", i, current["intents"][i]))
        value = {**old, "revision": old["revision"] + 1, "previous_hash": old["content_hash"],
                 "created_by_event": draft["event"]["id"]}
        value.pop("content_hash", None)
        for name in ("status", "kind", "title", "description", "rationale"):
            if getattr(args, name) is not None:
                value[name] = getattr(args, name)
        for arg, field in (("requirement", "requirements"), ("constraint", "constraints"), ("related", "related_intents")):
            if getattr(args, arg) is not None:
                value[field] = getattr(args, arg)
    if any(v["id"] == i for v in draft["intents"]):
        raise IntentError("intent already present in draft; edit it there")
    draft["intents"].append(value)
    save_draft(root, args.event, draft)
    return value


def add_binding(root, args):
    draft = load_draft(root, args.event)
    current = manifest(root)
    if args.command == "bind":
        i = identity(config(root), "binding", args.key)
        if i in current["bindings"]:
            raise IntentError("binding key already exists; use rebind")
        value = {"schema_version": 1, "id": i, "revision": 1, "previous_hash": None,
                 "intent_id": args.intent, "intent_revision": args.intent_revision,
                 "relationship": args.relationship, "status": args.status,
                 "rationale": args.rationale, "predecessors": args.predecessor or [],
                 "verification": {"status": args.verification_status or "declared"}}
    else:
        i = args.id
        if i not in current["bindings"]:
            raise IntentError(f"unknown binding {i}")
        old = read_json(root / record_path("binding", i, current["bindings"][i]))
        value = {**old, "revision": old["revision"] + 1, "previous_hash": old["content_hash"],
                 "status": args.status, "rationale": args.rationale}
        value.pop("content_hash", None)
        if args.intent_revision is not None:
            value["intent_revision"] = args.intent_revision
    if args.verification_status or args.verification_method or args.verification_evidence:
        verification = dict(value.get("verification") or {"status": "declared"})
        if args.verification_status:
            verification["status"] = args.verification_status
        if args.verification_method:
            verification["method"] = args.verification_method
        if args.verification_evidence:
            verification["evidence"] = args.verification_evidence
        value["verification"] = verification
    elif "verification" not in value:
        value["verification"] = {"status": "declared"}
    if args.path:
        if not safe_path(args.path) or metadata(args.path):
            raise IntentError("binding target must be a relative ordinary source path")
        entry = snapshot(root, staged=True).get(args.path)
        if entry is None:
            raise IntentError("target is absent from index; stage source before binding")
        source_id = put_object(root, entry[1])
        start = args.start if args.start is not None else 0
        end = args.end if args.end is not None else len(entry[1])
        value["target"] = {"path": args.path, "start": start, "end": end,
                           "source": source_id, "mode": entry[0]}
    elif "target" not in value:
        raise IntentError("bind requires --path")
    value["created_by_event"] = draft["event"]["id"]
    if any(v["id"] == i for v in draft["bindings"]):
        raise IntentError("binding already present in draft; edit it there")
    draft["bindings"].append(value)
    save_draft(root, args.event, draft)
    return value


def declare(root, args):
    draft = load_draft(root, args.event)
    current = manifest(root)
    new_intents = [v for v in draft["intents"] if v["id"] == args.intent]
    old_revision = current["intents"].get(args.intent)
    new_revision = new_intents[-1]["revision"] if new_intents else old_revision
    revisions = [b["revision"] for b in draft["bindings"] if b["id"] == args.binding]
    transition = {"intent_id": args.intent, "binding_id": args.binding, "action": args.action,
                  "from_revision": old_revision, "to_revision": new_revision,
                  "binding_revisions": revisions}
    transitions = draft["event"]["intent_transitions"]
    transitions[:] = [t for t in transitions if (t["intent_id"], t["binding_id"]) != (args.intent, args.binding)]
    transitions.append(transition)
    if args.provenance:
        draft["event"]["provenance"] = args.provenance
    save_draft(root, args.event, draft)
    return transition


def finalize(root, args):
    draft = load_draft(root, args.key)
    event = copy.deepcopy(draft["event"])
    current = manifest(root)
    if event["id"] in current["events"]:
        raise IntentError("event already finalized")
    event["records"] = []
    writes = {}
    for kind in ("intent", "binding"):
        for value in draft[kind + "s"]:
            value = seal(value)
            problems = check(value, SCHEMAS[kind])
            if problems:
                raise IntentError(str(problems))
            path = record_path(kind, value["id"], value["revision"])
            if (root / path).exists():
                raise IntentError(f"record already exists: {path}")
            writes[path] = canonical(value) + b"\n"
            event["records"].append({"type": kind, "id": value["id"], "revision": value["revision"]})
            current[kind + "s"][value["id"]] = value["revision"]
    event = seal(event)
    problems = check(event, SCHEMAS["event"])
    if problems:
        raise IntentError("incomplete draft: " + "; ".join(problems))
    writes[record_path("event", event["id"])] = canonical(event) + b"\n"
    current["events"].append(event["id"])
    writes[".intent/manifests/current.json"] = canonical(current) + b"\n"
    # Validate hypothetical staged source + working canonical metadata before writing.
    proposed = snapshot(root, staged=True)
    working = snapshot(root, working=True)
    proposed = {p: v for p, v in proposed.items() if not metadata(p)}
    proposed.update({p: v for p, v in working.items() if metadata(p)})
    proposed.update({p: ("100644", data) for p, data in writes.items()})
    base_head = head(root)
    base = snapshot(root, revision=base_head) if base_head else {}
    problems = validate(proposed, base)
    if problems:
        raise ValidationError(problems)
    for path, data in writes.items():
        write_json(root / path, loads(data), immutable=path != ".intent/manifests/current.json")
    return {"event": event["id"], "records": event["records"], "next": "stage .intent and run validate --staged"}


def validate_commit(root, commit, *, enforce_schema_definitions=True):
    parents = git(root, "rev-list", "--parents", "-n", "1", commit).decode().split()[1:]
    after = snapshot(root, revision=commit)
    if ".intent/config.json" not in after:
        if any(git(root, "ls-tree", "-z", parent, "--", ".intent/config.json") for parent in parents):
            return [{"code": "REMOVED_SYSTEM", "path": commit, "message": "intent system cannot be removed by deleting canonical history"}]
        return []  # Pre-adoption history, including legacy merges, is explicitly unknown.
    if len(parents) > 1:
        return [{"code": "MERGE_UNSUPPORTED", "path": commit, "message": "merge commits are unsupported; rebase or squash and reconcile metadata explicitly"}]
    before = snapshot(root, revision=parents[0]) if parents else {}
    return validate(after, before, enforce_schema_definitions=enforce_schema_definitions)


def run_validation(root, args):
    errors = []
    if args.push:
        for line in sys.stdin:
            fields = line.split()
            if len(fields) != 4:
                raise IntentError("invalid pre-push input")
            _, local, _, remote = fields
            if set(local) == {"0"}:
                continue
            revision_range = local if set(remote) == {"0"} else f"{remote}..{local}"
            commits = git(root, "rev-list", "--reverse", "--topo-order", revision_range).decode().split()
            for commit in commits:
                errors += validate_commit(root, commit)
    elif args.range:
        commits = git(root, "rev-list", "--reverse", "--topo-order", args.range).decode().split()
        for commit in commits:
            errors += validate_commit(root, commit)
    elif args.commit:
        errors = validate_commit(root, args.commit)
    else:
        # During a merge, comparing only HEAD misses constraints from the other parent.
        merge_head = git(root, "rev-parse", "--git-path", "MERGE_HEAD").decode().strip()
        merge_path = Path(merge_head) if Path(merge_head).is_absolute() else root / merge_head
        if merge_path.exists():
            errors.append({"code": "MERGE_UNSUPPORTED", "path": "MERGE_HEAD", "message": "abort merge and rebase or squash; multi-parent reconciliation is not implemented"})
        else:
            base_head = head(root)
            before = snapshot(root, revision=args.base or base_head) if (args.base or base_head) else {}
            after = snapshot(root, staged=args.staged, working=not args.staged)
            errors = validate(after, before)
    return {"valid": not errors, "errors": errors}


def location(value):
    try:
        path, line = value.rsplit(":", 1)
        line = int(line)
        if line < 1 or not safe_path(path):
            raise ValueError()
        return path, line
    except ValueError as exc:
        raise IntentError("location must be repository/relative/path:LINE (one-based)") from exc


def line_range(data, line):
    starts = [0] + [i + 1 for i, b in enumerate(data) if b == 10]
    if line > len(starts):
        raise IntentError(f"line {line} is outside snapshot")
    return starts[line - 1], starts[line] if line < len(starts) else len(data)


def query(root, args):
    entries = snapshot(root, revision=args.at) if getattr(args, "at", None) else snapshot(root, working=True)
    store = Store(entries)
    if store.errors:
        raise IntentError(str(store.errors))
    intents, bindings = store.current("intent"), store.current("binding")
    if args.command in ("show-intent", "show-binding"):
        kind = args.command[5:]
        collection = getattr(store, kind + "s")
        revisions = sorted(r for i, r in collection if i == args.id)
        if not revisions:
            raise IntentError("unknown identity")
        return collection[(args.id, args.revision or revisions[-1])]
    if args.command in ("status", "doctor"):
        stale = [b["id"] for b in bindings.values() if b["status"] in ("active", "unresolved") and
                 b["intent_revision"] != store.manifest["intents"].get(b["intent_id"])]
        if args.command == "doctor":
            result = run_validation(root, argparse.Namespace(push=False, range=None, commit=None, base=None, staged=False))
            return {**result, "reassessment": stale, "repair": "repair drafts; append corrective revisions for committed history"}
        return {"intents": len(intents), "bindings": len(bindings), "events": len(store.events),
                "reassessment": stale, "unresolved": [b["id"] for b in bindings.values() if b["status"] == "unresolved"]}
    if args.command == "impact":
        if args.id not in intents:
            raise IntentError("unknown intent")
        related, pending = set(), [args.id]
        while pending:
            i = pending.pop()
            if i in related:
                continue
            related.add(i)
            pending += intents[i]["related_intents"]
            pending += [j for j, v in intents.items() if i in v["related_intents"]]
        return {"intent": args.id, "related_intents": sorted(related - {args.id}),
                "bindings": [{**b, "needs_reassessment": b["intent_revision"] != store.manifest["intents"][b["intent_id"]]}
                             for b in bindings.values() if b["intent_id"] in related and b["status"] in ("active", "unresolved")]}
    path, line = location(args.location)
    entry = entries.get(path)
    if entry is None:
        if args.command == "explain":
            raise IntentError("path does not exist in selected snapshot; use history or --at REV")
        start, end = 0, 0
    else:
        start, end = line_range(entry[1], line)
    from .core import touches
    source_map = source(entries)
    selected = []
    for b in bindings.values():
        live = live_target(b, source_map, store.objects) if b["status"] in ("active", "unresolved") else None
        coords = live if live is not None else b["target"]
        if coords["path"] == path and touches(start, end, coords["start"], coords["end"]):
            selected.append((b, live))
    if args.command == "explain":
        result = []
        for b, live in selected:
            if b["status"] not in ("active", "unresolved"):
                continue
            governed = store.intents[(b["intent_id"], b["intent_revision"])]
            result.append({"binding": b, "intent": governed,
                           "current_intent": intents[b["intent_id"]],
                           "needs_reassessment": governed["revision"] != intents[b["intent_id"]]["revision"],
                           "target_matches": ref(entry) == {"source": b["target"]["source"], "mode": b["target"]["mode"]},
                           "live_target": ({"path": live["path"], "start": live["start"], "end": live["end"],
                                            "mode": live["mode"]} if live else None),
                           "verification": verification_of(b),
                           "origin_event": store.events[b["created_by_event"]]})
        return {"path": path, "line": line, "applicable": result,
                "provenance": "known where bound; unbound regions have unknown provenance; new bytes require intent"}
    # Explicit lineage plus spatial matches in each historical snapshot. Never infer moves.
    selected_ids = {b["id"] for b, _ in selected}
    for b in store.bindings.values():
        target = b["target"]
        if target["path"] == path:
            try:
                a, z = line_range(store.objects[target["source"]], line)
                if touches(a, z, target["start"], target["end"]):
                    selected_ids.add(b["id"])
            except (KeyError, IntentError):
                pass
    changed = True
    while changed:
        changed = False
        for b in store.bindings.values():
            linked = {b["id"], *b["predecessors"]}
            if linked & selected_ids and not linked <= selected_ids:
                selected_ids |= linked
                changed = True
    selected_intents = {b["intent_id"] for b in store.bindings.values() if b["id"] in selected_ids}
    events = [store.events[i] for i in store.manifest["events"]
              if any(t["intent_id"] in selected_intents or t["binding_id"] in selected_ids for t in store.events[i]["intent_transitions"])
              or any(c["path"] == path for c in store.events[i]["changes"])]
    return {"path": path, "line": line, "events": events,
            "intent_revisions": [v for (i, r), v in sorted(store.intents.items()) if i in selected_intents],
            "binding_revisions": [v for (i, r), v in sorted(store.bindings.items()) if i in selected_ids],
            "current_bindings": [bindings[i] for i in sorted(selected_ids) if i in bindings],
            "baseline_provenance": "unknown",
            "selection": "explicit binding lineage, live coordinate continuity, and spatial matches at each historical snapshot; line numbers alone do not establish identity"}


def parser():
    p = argparse.ArgumentParser(description="Intent-native Git overlay (Python standard library). "
                                "Optional: `intentctl trueup --help` retrofits non-authoritative retrospective intent from Git history.")
    sub = p.add_subparsers(dest="command", required=True)
    def command(name):
        cmd = sub.add_parser(name)
        cmd.add_argument("--json", action="store_true")
        return cmd
    init = command("init")
    init.add_argument("--no-hooks", action="store_true")
    init.add_argument("--no-instructions", action="store_true", help="initialize metadata without deploying instruction files")
    d = command("deploy")
    d.add_argument("--update", action="store_true", help="replace an intentctl-managed INTENT.md")
    for name in ("audit", "reconcile", "watch"):
        c = command(name)
        c.add_argument("--since", help="audit commits after this ancestor instead of all history")
        if name == "watch":
            c.add_argument("--interval", type=float, default=5)
            c.add_argument("--once", action="store_true")
    command("report")
    command("uninstall-hooks")
    command("baseline")
    v = command("validate")
    group = v.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true")
    group.add_argument("--push", action="store_true")
    group.add_argument("--range")
    group.add_argument("--commit")
    v.add_argument("--base")
    for name in ("status", "doctor", "explain", "history", "show-intent", "show-binding", "impact"):
        q = command(name)
        q.add_argument("--at", help="query committed snapshot REV instead of working tree")
        if name in ("explain", "history"):
            q.add_argument("location")
            q.add_argument("--trueup-state", help="true-up state directory (default: <git-common-dir>/intent-trueup, or $INTENTCTL_TRUEUP_STATE_DIR)")
            q.add_argument("--no-retrospective", action="store_true",
                           help="omit the optional non-authoritative `intentctl trueup` section even if a true-up store exists")
        elif name in ("show-intent", "show-binding", "impact"):
            q.add_argument("id")
            if name != "impact":
                q.add_argument("--revision", type=int)
    c = command("capture")
    c.add_argument("--key", required=True)
    c.add_argument("--description", required=True)
    c.add_argument("--rationale", required=True)
    c.add_argument("--kind", default="code-modification")
    c.add_argument("--provenance", choices=["known", "unknown"], help="explicit provenance declaration; never inferred")
    f = command("finalize")
    f.add_argument("key")
    for name in ("intent-create", "intent-revise"):
        c = command(name)
        c.add_argument("--event", required=True)
        if name == "intent-create":
            c.add_argument("--key", required=True)
        else:
            c.add_argument("id")
        for flag in ("title", "description", "rationale"):
            c.add_argument("--" + flag, required=name == "intent-create")
        c.add_argument("--kind", default="requirement" if name == "intent-create" else None)
        c.add_argument("--status", choices=["active", "retired", "superseded"], default="active" if name == "intent-create" else None)
        for flag in ("requirement", "constraint", "related"):
            c.add_argument("--" + flag, action="append")
    for name in ("bind", "rebind"):
        c = command(name)
        c.add_argument("--event", required=True)
        if name == "bind":
            c.add_argument("--key", required=True)
            c.add_argument("--intent", required=True)
            c.add_argument("--relationship", choices=SCHEMAS["binding"]["properties"]["relationship"]["enum"], default="implements")
            c.add_argument("--predecessor", action="append")
        else:
            c.add_argument("id")
        c.add_argument("--intent-revision", type=int, default=1 if name == "bind" else None)
        c.add_argument("--path")
        c.add_argument("--start", type=int)
        c.add_argument("--end", type=int)
        c.add_argument("--rationale", required=True)
        c.add_argument("--status", choices=["active", "ended", "historical", "unresolved"], default="active")
        c.add_argument("--verification-status", choices=["declared", "pending", "verified", "failed", "unverifiable"],
                       help="separate claimed preservation from independently supported verification")
        c.add_argument("--verification-method", help="how verification was performed when status is verified")
        c.add_argument("--verification-evidence", help="evidence reference for verified/failed/unverifiable claims")
    c = command("declare")
    c.add_argument("--event", required=True)
    c.add_argument("--intent", required=True)
    c.add_argument("--binding")
    c.add_argument("--action", choices=ACTIONS, required=True)
    c.add_argument("--provenance", choices=["known", "unknown"])
    return p


def retrospective(root, args, result):
    """Attach labeled non-authoritative true-up discoveries to explain/history output when a store exists.

    Optional and failure-isolated: the canonical answer is never changed or blocked by this section."""
    if getattr(args, "no_retrospective", False) or not isinstance(result, dict):
        return result
    try:
        from .trueup import query as trueup_query
        from .trueup.gitscan import Repo
        repo = Repo(root)
        store = trueup_query.open_store(repo, getattr(args, "trueup_state", None), must_exist=False)
        if store is None:
            return result
        path, line = location(args.location)
        result["retrospective"] = trueup_query.retrospective_section(repo, store, path, line, getattr(args, "at", None))
    except Exception as exc:  # noqa: BLE001 - the canonical query must still succeed
        result["retrospective"] = {"available": False, "authority": "non-authoritative",
                                   "error": f"true-up lookup failed and was skipped: {type(exc).__name__}: {str(exc)[:200]}"}
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["trueup"]:
        from .trueup.cli import main as trueup_main
        return trueup_main(argv[1:])
    args = parser().parse_args(argv)
    try:
        root = root_path()
        if args.command == "init":
            from .adoption import deployment_plan, deploy
            if not args.no_instructions:
                deployment_plan(root)  # Check conflicts before creating metadata or hooks.
            result = initialize(root, args.no_hooks)
            if not args.no_instructions:
                result["instructions"] = deploy(root)
        elif args.command == "deploy":
            from .adoption import deploy
            result = deploy(root, args.update)
        elif args.command in ("audit", "reconcile", "watch"):
            from .adoption import audit, reconciliation
            if args.command == "watch":
                import math
                if not math.isfinite(args.interval) or args.interval <= 0:
                    raise IntentError("--interval must be a positive finite number")
                previous = None
                cache = {}
                try:
                    while True:
                        try:
                            result = audit(root, args.since, cache=cache)
                        except ValidationError as exc:
                            result = {"needs_attention": True, "valid": False, "errors": exc.errors}
                        except (IntentError, OSError, KeyError, IndexError) as exc:
                            result = {"needs_attention": True, "valid": False, "errors": [
                                {"code": "OPERATION", "path": ".intent", "message": str(exc)}]}
                        fingerprint = canonical(result)
                        if fingerprint != previous:
                            output(result, args.json)
                            sys.stdout.flush()
                            previous = fingerprint
                        if args.once:
                            return int(result["needs_attention"])
                        time.sleep(args.interval)
                except KeyboardInterrupt:
                    return 0
            result = reconciliation(root, args.since) if args.command == "reconcile" else audit(root, args.since)
            output(result, args.json)
            return int(result.get("audit", result)["needs_attention"])
        elif args.command == "report":
            from .adoption import report, review_state
            if not args.json:
                print(report(root), end="")
                return 0
            result = review_state(root)
        elif args.command == "uninstall-hooks":
            install_hooks(root, uninstall=True)
            result = {"hooks": "uninstalled; original hooks restored"}
        elif args.command == "baseline":
            result = make_baseline(root)
        elif args.command == "capture":
            result = capture(root, args)
        elif args.command in ("intent-create", "intent-revise"):
            result = add_intent(root, args)
        elif args.command in ("bind", "rebind"):
            result = add_binding(root, args)
        elif args.command == "declare":
            result = declare(root, args)
        elif args.command == "finalize":
            result = finalize(root, args)
        elif args.command == "validate":
            result = run_validation(root, args)
        else:
            result = query(root, args)
            if args.command in ("explain", "history"):
                result = retrospective(root, args, result)
        output(result, args.json)
        return 1 if isinstance(result, dict) and result.get("valid") is False else 0
    except ValidationError as exc:
        output({"valid": False, "errors": exc.errors}, args.json)
        return 1
    except (IntentError, OSError, KeyError, IndexError) as exc:
        result = {"valid": False, "errors": [{"code": "OPERATION", "path": ".intent", "message": str(exc)}]}
        output(result, args.json)
        return 1
