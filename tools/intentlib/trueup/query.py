"""Read-only views over the true-up store, clearly separated from canonical records."""
import os
from pathlib import Path

from . import NON_AUTHORITATIVE_LABEL
from . import gitscan, graph as graph_mod
from .store import Store, default_state_dir
from .util import TrueUpError, iso, read_json, write_json

REVIEW_REMINDER = ("Retrospective claims are leads for investigation. Read the cited evidence, check the current code, and do not "
                   "treat any of this as a requirement unless a maintainer promotes it into canonical intent.")


def evidence_link(source):
    if not source:
        return None
    if source.get("url"):
        return source["url"]
    if source.get("commit"):
        return f"git:commit/{source['commit']}" + (f"#{source['path']}" if source.get("path") else "")
    return None


def finding_view(finding, detail):
    evidence = []
    for e in finding["evidence"]:
        src = e.get("source") or {}
        item = {"ref": e["ref"], "type": src.get("type"), "link": evidence_link(src), "commit": src.get("commit"),
                "path": src.get("path"), "hunk": src.get("hunk"), "author": src.get("author"), "created_at": src.get("created_at"),
                "quote_verified": e.get("quote_verified", False)}
        if detail or e.get("quote_verified"):
            quote = e.get("quote")
            item["quote"] = quote if detail or not quote or len(quote) <= 200 else quote[:200] + "..."
        evidence.append(item)
    return {"id": finding["id"], "kind": finding["kind"], "title": finding["title"], "statement": finding["statement"],
            "claim_type": "direct statement supported by a verified quote" if finding["basis"] == "explicit" else "inference (not stated directly)",
            "basis": finding["basis"], "uncertainty": finding["uncertainty"], "uncertainty_notes": finding.get("uncertainty_notes"),
            "missing_evidence": finding.get("missing_evidence"), "change_hint": finding.get("change_hint"),
            "commit": finding["commit"], "subject": finding.get("subject"), "date": finding.get("date"), "paths": finding["paths"],
            "tags": finding["tags"], "evidence": evidence, "analysis": finding.get("analysis"), "merged_from": finding.get("merged_from")}


def node_view(node, detail=False, applicability=None, association=None):
    latest = node["findings_data"][-1]
    view = {
        "id": node["id"], "title": node["title"], "kind": node["kind"],
        "historical_lifecycle": node["lifecycle"], "authority": "non-authoritative",
        "current_statement": latest["statement"], "paths": node["paths"], "tags": node["tags"],
        "first_commit": node["first_commit"], "last_commit": node["last_commit"],
        "timeline": [{"commit": t["commit"], "date": t.get("date"), "role": t["role"], "relation": t.get("relation"), "finding": t["finding"]}
                     for t in node["timeline"]],
        "findings": [finding_view(f, detail) for f in node["findings_data"]],
        "supporting_findings": [finding_view(f, detail) for f in node["supporting_data"]],
        "ended_by": node["ended_by"], "contested_by": node["contested_by"],
        "edges": [{k: v for k, v in e.items() if k != "evidence" or detail} for e in node["edges"]],
        "content_hash": graph_mod.node_hash(node),
    }
    if applicability is not None:
        view["present_applicability"] = applicability
    if association:
        view["association"] = association
    return view


def open_store(repo, state_dir=None, must_exist=True):
    state_dir = state_dir or os.environ.get("INTENTCTL_TRUEUP_STATE_DIR")
    path = Path(state_dir) if state_dir else default_state_dir(repo)
    if not Store.exists(path):
        if must_exist:
            raise TrueUpError(f"no true-up store at {path}; run `intentctl trueup init`")
        return None
    return Store.open(path)


def cached_coverage(store):
    return read_json(store.path / "coverage.json", default={})


def write_coverage_cache(store, report):
    write_json(store.path / "coverage.json", {"as_of": iso(), **report})


def compact_coverage(report):
    if not report:
        return {"available": False, "note": "no coverage snapshot yet; run `intentctl trueup status`"}
    return {"as_of": report.get("as_of"), "commits_in_scope": report["commits_in_scope"], "history_complete": report["manifest"]["history_complete"],
            "history_notes": report["manifest"]["notes"], "inspection": report["inspection"], "ai_analysis": report["ai_analysis"],
            "external_evidence": report["external_evidence"], "linking": report["linking"], "complete": report["complete"],
            "incomplete_total": report["incomplete_total"],
            "interpretation": "discoveries exist only for commits whose analysis succeeded; incomplete coverage means absence of a "
                              "discovery is not evidence that no decision was made"}


def retrospective_section(repo, store, path, line, at=None, detail=False, max_nodes=12, timeout=60):
    """Retrospective discoveries for PATH:LINE at a snapshot (default HEAD)."""
    graph = graph_mod.load_graph(store)
    revision = at or "HEAD"
    limitations = []
    section = {"label": NON_AUTHORITATIVE_LABEL, "authority": "non-authoritative", "guidance": REVIEW_REMINDER,
               "store": {"path": str(store.path), "id": store.meta()["store_id"], "graph_built_at": getattr(graph, "built_at", None)},
               "coverage": compact_coverage(cached_coverage(store))}
    names = gitscan.path_history_names(repo, revision, path, timeout)
    if names is None:
        names = [{"path": path, "commit": None, "similarity": None}]
        limitations.append("Git rename history unavailable; only the current path name was matched")
    commits = gitscan.region_commits(repo, revision, path, line, timeout)
    if commits is None:
        limitations.append("line-level history (git log -L) unavailable for this location; results are path-level only")
    dirty = False
    if not at:
        dirty = repo.toplevel is not None and repo.run("diff", "--name-only", "HEAD", "--", path, ok=(0, 128)).strip() != b""
        if dirty:
            limitations.append(f"{path} has uncommitted changes; line history was computed against HEAD, so the line number may be offset")
    section["query"] = {"path": path, "line": line, "revision": revision, "line_history_commits": None if commits is None else len(commits),
                        "path_names_considered": names, "worktree_modified": dirty,
                        "method": "line-history = nodes citing commits that touched this line per `git log -L`; path-history = nodes citing "
                                  "this path or earlier names per Git rename detection (a heuristic, never identity)"}
    seen, picked = set(), []
    for node in graph.nodes_for_commits(commits or []):
        seen.add(node["id"])
        picked.append((node, "line-history"))
    all_names = [n["path"] for n in names]
    for node in graph.nodes_for_path(all_names):
        if node["id"] not in seen:
            seen.add(node["id"])
            renamed = any(n["path"] != path and n["path"] in node["paths"] for n in names)
            picked.append((node, "path-history (earlier name via rename detection; proposed)" if renamed else "path-history"))
    cache = {}
    picked.sort(key=lambda p: (0 if p[1] == "line-history" else 1, -p[0]["timeline"][-1]["index"]))
    views = []
    for node, how in picked[:max_nodes]:
        try:
            appl = graph_mod.applicability(repo, node, revision, cache, gitscan)
        except Exception as exc:
            appl = {"status": "unknown", "verified": False, "basis": [f"reconciliation failed: {exc}"]}
        views.append(node_view(node, detail, appl, how))
    if len(picked) > max_nodes:
        limitations.append(f"{len(picked) - max_nodes} further related discoveries omitted; use `trueup list --path`")
    cov = section["coverage"]
    if cov.get("available") is not False and not cov.get("complete"):
        limitations.append("true-up coverage is incomplete (see coverage); missing discoveries do not mean no decision was made")
    if not graph.nodes:
        limitations.append("the true-up store has no discoveries yet")
    section["discoveries"] = views
    section["limitations"] = limitations
    return section


def render_text(section):
    lines = [section["label"], ""]
    q = section["query"]
    lines.append(f"Location: {q['path']}:{q['line']} @ {q['revision']}  (line-history commits: {q['line_history_commits']})")
    cov = section["coverage"]
    if cov.get("commits_in_scope") is not None:
        lines.append(f"Coverage: {cov['commits_in_scope']} commits in scope; analysed ok "
                     f"{cov['ai_analysis'].get('analyzed', 0) + cov['ai_analysis'].get('no_intent_found', 0)}, partial {cov['ai_analysis'].get('partial', 0)}, "
                     f"failed {cov['ai_analysis'].get('failed', 0)}, pending {cov['ai_analysis'].get('pending', 0)}; complete={cov['complete']}")
    if not section["discoveries"]:
        lines.append("\nNo retrospective discoveries match this location.")
    for d in section["discoveries"]:
        appl = d.get("present_applicability", {})
        lines += ["", f"[{d['kind']}] {d['title']}   ({d['id']})",
                  f"  association: {d.get('association')}",
                  f"  history: {d['historical_lifecycle']}; present applicability (unverified): {appl.get('status', 'unknown')}",
                  f"  statement: {d['current_statement']}"]
        for f in d["findings"]:
            lines.append(f"  - {f['date'] or '?'} commit {f['commit'][:12]} [{f['basis']}, uncertainty {f['uncertainty']}] {f['title']}")
            for e in f["evidence"][:3]:
                quote = f" \"{e['quote']}\"" if e.get("quote") else ""
                lines.append(f"      evidence {e['ref']} {e['link'] or ''}{quote}")
        for end in d["ended_by"]:
            lines.append(f"  ended by: {end['relation']} in commit {end['commit'][:12]}")
        if d["contested_by"]:
            lines.append(f"  contested by: {', '.join(c['node'] for c in d['contested_by'])}")
    if section["limitations"]:
        lines += ["", "Limitations:"] + [f"  - {l}" for l in section["limitations"]]
    return "\n".join(lines)


def render_tree(graph, limit=None):
    """Plain-text evolution view: one block per node, newest edges listed beneath it."""
    lines, nodes = [], sorted(graph.nodes.values(), key=lambda n: n["timeline"][0]["index"])
    for node in nodes[:limit]:
        lines.append(f"{node['id']} [{node['kind']}] {node['title']}  <{node['lifecycle']}>")
        for t in node["timeline"]:
            lines.append(f"    {t.get('date') or '?'} {t['commit'][:12]} {t['role']}" + (f" ({t['relation']})" if t.get("relation") else ""))
        for e in node["edges"]:
            lines.append(f"    -> {e['type']} {e['to']} (commit {e['commit'][:12]}, {e['source']})")
        for e in node["ended_by"]:
            lines.append(f"    ended by {e['relation']} via {e['node']}")
    return "\n".join(lines)


def suggest_bindings(repo, node, target_rev, limit=6):
    """Suggestions only: lines at the snapshot whose blame commit is cited by this discovery."""
    commits = {t["commit"] for t in node["timeline"]}
    suggestions = []
    for path in node["paths"]:
        if not repo.path_exists(target_rev, path):
            continue
        blame = gitscan.blame_lines(repo, target_rev, path)
        if not blame:
            suggestions.append({"path": path, "lines": None, "basis": "path exists at snapshot; line attribution unavailable"})
            continue
        runs, start, prev = [], None, None
        for number in sorted(blame):
            if blame[number] in commits:
                if start is None:
                    start = number
                elif number != prev + 1:
                    runs.append((start, prev))
                    start = number
                prev = number
            elif start is not None:
                runs.append((start, prev))
                start = None
        if start is not None:
            runs.append((start, prev))
        if runs:
            for a, z in runs[:limit]:
                suggestions.append({"path": path, "lines": [a, z], "basis": "git blame attributes these lines to commits cited by this discovery; "
                                                                            "may be incidental or since superseded"})
        else:
            suggestions.append({"path": path, "lines": None, "basis": "path exists, but no current line is attributed to a cited commit"})
    return suggestions
