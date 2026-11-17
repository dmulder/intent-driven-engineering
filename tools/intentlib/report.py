"""Read-only, agent-facing reports derived from canonical records.

Nothing here writes metadata or caches state: every answer is recomputed from the
selected snapshot so the repository stays the single source of truth.
"""
from .core import IntentError, live_target, safe_path, source, verification_of

ACTIVE = ("active", "unresolved")
SEVERITIES = ("must", "should", "prefer")
# Records written before severity existed bind all work, so absence means "must".
DEFAULT_SEVERITY = "must"
REPORTS = ("context", "coverage", "list-intents")


def severity_of(intent):
    return intent.get("severity", DEFAULT_SEVERITY)


def summary(intent):
    return {"id": intent["id"], "revision": intent["revision"], "status": intent["status"],
            "kind": intent["kind"], "severity": severity_of(intent), "title": intent["title"],
            "description": intent["description"], "rationale": intent["rationale"],
            "requirements": intent["requirements"], "constraints": intent["constraints"],
            "related_intents": intent["related_intents"]}


def brief(intent, assessed_revision):
    """Compact reminder shown where an agent must decide how to treat an intent."""
    return {"title": intent["title"], "severity": severity_of(intent), "status": intent["status"],
            "revision": intent["revision"], "assessed_revision": assessed_revision,
            "needs_reassessment": assessed_revision != intent["revision"],
            "requirements": intent["requirements"], "constraints": intent["constraints"],
            "rationale": intent["rationale"]}


def line_of(data, offset):
    return data.count(b"\n", 0, offset) + 1


def span(data, start, end):
    first = line_of(data, start)
    return {"start": start, "end": end, "start_line": first,
            "end_line": line_of(data, end - 1) if end > start else first}


def merge(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def gaps(length, merged):
    cursor, out = 0, []
    for start, end in merged:
        if start > cursor:
            out.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < length:
        out.append((cursor, length))
    return out


def resolve_scope(paths, source_map):
    """Expand file and directory arguments to governed source paths; fail on no match."""
    selected = set()
    for raw in paths or [""]:
        path = raw[2:] if raw.startswith("./") else raw
        path = path.rstrip("/")
        if path in ("", "."):
            selected.update(source_map)
            continue
        if not safe_path(path):
            raise IntentError(f"{raw}: scope must be a repository-relative path or directory")
        matches = [p for p in source_map if p == path or p.startswith(path + "/")]
        if not matches:
            raise IntentError(f"{raw}: no governed source matches in this snapshot; use history or --at REV")
        selected.update(matches)
    return sorted(selected)


def live_bindings(path, bindings, source_map, objects):
    for binding in bindings.values():
        if binding["status"] in ACTIVE and binding["target"]["path"] == path:
            yield binding, live_target(binding, source_map, objects)


def file_context(path, entry, store, intents, bindings, source_map):
    data = entry[1]
    applicable, ranges = [], []
    for binding, live in live_bindings(path, bindings, source_map, store.objects):
        governed = store.intents[(binding["intent_id"], binding["intent_revision"])]
        current = intents[binding["intent_id"]]
        item = {"binding": {"id": binding["id"], "revision": binding["revision"],
                            "relationship": binding["relationship"], "status": binding["status"],
                            "rationale": binding["rationale"], "verification": verification_of(binding)},
                "intent": summary(governed), "needs_reassessment": governed["revision"] != current["revision"],
                "live": live is not None,
                "range": span(data, live["start"], live["end"]) if live else None}
        if item["needs_reassessment"]:
            item["current_intent"] = summary(current)
        if live:
            ranges.append((live["start"], live["end"]))
        applicable.append(item)
    applicable.sort(key=lambda i: (SEVERITIES.index(i["intent"]["severity"]),
                                   i["range"]["start"] if i["range"] else -1, i["binding"]["id"]))
    merged = merge(ranges)
    covered = sum(end - start for start, end in merged)
    return {"path": path, "bytes": len(data), "covered_bytes": covered,
            "applicable": applicable,
            "uncovered": [span(data, a, z) for a, z in gaps(len(data), merged)]}


def related(files, intents):
    """Active intents one hop from the applicable ones: logical neighbors that bind elsewhere."""
    present = {item["intent"]["id"] for f in files for item in f["applicable"]}
    found = {}
    for i in sorted(present):
        neighbors = set(intents[i]["related_intents"]) | {j for j, v in intents.items() if i in v["related_intents"]}
        for j in sorted(neighbors - present):
            if j in intents and intents[j]["status"] == "active" and j not in found:
                found[j] = {"id": j, "title": intents[j]["title"], "severity": severity_of(intents[j]),
                            "via": i}
    return list(found.values())


def context(store, entries, args):
    source_map = source(entries)
    intents, bindings = store.current("intent"), store.current("binding")
    paths = resolve_scope(args.paths, source_map)
    files = [file_context(p, source_map[p], store, intents, bindings, source_map) for p in paths]
    return {"scope": args.paths or ["."], "files": files, "related_intents": related(files, intents),
            "summary": {"files": len(files),
                        "applicable_bindings": sum(len(f["applicable"]) for f in files),
                        "needs_reassessment": sum(1 for f in files for i in f["applicable"] if i["needs_reassessment"]),
                        "uncovered_files": [f["path"] for f in files if f["bytes"] and not f["covered_bytes"]]},
            "provenance": "bytes outside every applicable range have unknown provenance; new bytes require intent"}


def coverage(store, entries, args):
    source_map = source(entries)
    bindings = store.current("binding")
    rows = []
    for path in resolve_scope(args.paths, source_map):
        found = [(b, live) for b, live in live_bindings(path, bindings, source_map, store.objects)]
        merged = merge([(live["start"], live["end"]) for _, live in found if live])
        size = len(source_map[path][1])
        covered = sum(end - start for start, end in merged)
        rows.append({"path": path, "bytes": size, "covered_bytes": covered,
                     "percent": covered * 100 // size if size else None,
                     "active_bindings": len(found),
                     "intents": sorted({b["intent_id"] for b, _ in found})})
    sized = [r for r in rows if r["bytes"]]
    total, covered = sum(r["bytes"] for r in sized), sum(r["covered_bytes"] for r in sized)
    return {"files": rows,
            "summary": {"files": len(rows), "files_with_intent": sum(1 for r in rows if r["covered_bytes"]),
                        "uncovered_files": [r["path"] for r in sized if not r["covered_bytes"]],
                        "bytes": total, "covered_bytes": covered,
                        "percent": covered * 100 // total if total else None}}


def list_intents(store, entries, args):
    intents, bindings = store.current("intent"), store.current("binding")
    needle = (args.contains or "").lower()
    rows = []
    for i, intent in intents.items():
        if args.status != "all" and intent["status"] != args.status:
            continue
        text = " ".join([intent["title"], intent["description"], intent["rationale"],
                         *intent["requirements"], *intent["constraints"]]).lower()
        if needle not in text:
            continue
        mine = [b for b in bindings.values() if b["intent_id"] == i and b["status"] in ACTIVE]
        rows.append({"id": i, "revision": intent["revision"], "status": intent["status"],
                     "kind": intent["kind"], "severity": severity_of(intent), "title": intent["title"],
                     "active_bindings": len(mine),
                     "files": sorted({b["target"]["path"] for b in mine}),
                     "needs_reassessment": sum(1 for b in mine if b["intent_revision"] != intent["revision"])})
    rows.sort(key=lambda r: (SEVERITIES.index(r["severity"]), r["title"], r["id"]))
    total = len(rows)
    if args.limit is not None:
        rows = rows[:args.limit]
    return {"intents": rows, "total": total, "truncated": len(rows) < total}


def freshness(store):
    """Decay signals: Telos found stale rules actively mislead agents, so surface them."""
    intents, bindings = store.current("intent"), store.current("binding")
    live = [b for b in bindings.values() if b["status"] in ACTIVE]
    stale = [b["id"] for b in live if b["intent_revision"] != intents[b["intent_id"]]["revision"]]
    governing = {b["intent_id"] for b in live}
    orphaned = sorted(i for i, v in intents.items() if v["status"] == "active" and i not in governing)
    counts = {}
    for b in live:
        status = verification_of(b)["status"]
        counts[status] = counts.get(status, 0) + 1
    result = {"reassessment": stale,
              "unresolved": [b["id"] for b in bindings.values() if b["status"] == "unresolved"],
              "orphaned_intents": orphaned,
              "verification": dict(sorted(counts.items())),
              "failed_verification": [b["id"] for b in live if verification_of(b)["status"] == "failed"]}
    result["attention"] = {k: len(result[k]) for k in
                           ("reassessment", "unresolved", "orphaned_intents", "failed_verification") if result[k]}
    return result


def run(command, store, entries, args):
    return {"context": context, "coverage": coverage, "list-intents": list_intents}[command](store, entries, args)
