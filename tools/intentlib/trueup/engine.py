"""The resumable true-up run: per-commit stages with durable checkpoints and failure isolation.

Stages per commit: inspect (local Git) -> external (forge evidence, optional) -> analyse (AI,
optional) -> link (ordered, integrates findings into the graph). Each stage persists before the
next begins, a commit is marked analysed only after its attempt file is durable, and an exception
in one commit or request is recorded against that commit and never stops the run. Only an
unwritable/corrupt state directory ends a run early (FatalStateError).
"""
import concurrent.futures
import threading
import time
import uuid

from . import ANALYSIS_VERSION, PROMPT_VERSION
from . import analysis, forge as forge_mod, gitscan, graph as graph_mod
from .providers import (BudgetExhausted, CallFailed, Caller, OversizeError, Circuits)
from .util import FatalStateError, TrueUpError, fault, iso, now, redact

TERMINAL_ANALYSIS = ("analyzed", "no_intent_found")
EXIT_COMPLETE, EXIT_FATAL, EXIT_LIMIT, EXIT_INCOMPLETE = 0, 1, 3, 4


def blank_commit(item):
    return {"sha": item["sha"], "index": item["index"],
            "inspection": {"status": "pending"}, "external": {"status": "pending"},
            "analysis": {"status": "pending"}, "link": {"status": "pending"}, "errors": [], "epoch": 0}


def record_error(rec, stage, error_class, message, retryable=True):
    text, _ = redact(str(message))
    rec["errors"].append({"stage": stage, "class": error_class, "message": text[:400], "at": iso(), "retryable": retryable})
    del rec["errors"][:-20]


def date_of(item):
    return iso(item["ts"]) if item.get("ts") is not None else None


class Engine:
    def __init__(self, store, repo, config, providers=(), caller=None, forge_evidence=None, jobs=1, stop=None,
                 out=None, limits=None):
        self.store, self.repo, self.config = store, repo, config
        self.providers = list(providers)
        self.caller = caller
        self.forge = forge_evidence
        self.jobs = max(1, jobs)
        self.stop = stop or threading.Event()
        self.out = out or (lambda msg: None)
        self.limits = {**analysis.DEFAULT_LIMITS, **(limits or config.get("limits", {}))}
        self.ai = bool(self.providers) and caller is not None
        self.network = bool(forge_evidence and forge_evidence.enabled)
        self.counters = {"started": 0, "inspected": 0, "analyzed": 0, "failed_commits": 0, "chunks_reused": 0}
        self.stop_reason = None
        self._counter_lock = threading.Lock()
        self.manifest = store.manifest()
        self.items = {c["sha"]: c for c in self.manifest["commits"]}

    def bump(self, key, n=1):
        with self._counter_lock:
            self.counters[key] = self.counters.get(key, 0) + n

    # -- selection -------------------------------------------------------------------------
    def select(self, mode="pending", shas=None, stages=("inspection", "external", "analysis", "link"), include_partial=False):
        """Commits that still need work. `pending` = never attempted; `retry` = failed (and optionally partial)."""
        wanted = set(shas) if shas else None
        selected = []
        for item in self.manifest["commits"]:
            if wanted is not None and item["sha"] not in wanted:
                continue
            rec = self.store.commit(item["sha"]) or blank_commit(item)
            if self._needs(rec, mode, stages, include_partial):
                selected.append(item)
        return selected

    def _needs(self, rec, mode, stages, include_partial):
        bad = ("failed", "partial") if include_partial else ("failed",)
        if mode == "pending":
            if rec["inspection"]["status"] == "pending":
                return True
            if self.network and rec["external"]["status"] in ("pending", "not_requested"):
                return True
            if self.ai and rec["analysis"]["status"] == "pending":
                return True
            if self.ai and rec["link"]["status"] == "pending" and rec["analysis"]["status"] in ("analyzed", "partial"):
                return True
            return False
        if "inspection" in stages and rec["inspection"]["status"] in bad:
            return True
        if "external" in stages and self.network and rec["external"]["status"] in bad:
            return True
        if "analysis" in stages and self.ai and (rec["analysis"]["status"] in bad or rec["analysis"]["status"] == "pending"):
            return True
        if "link" in stages and self.ai and rec["link"]["status"] in ("failed", "stale"):
            return True
        return False

    # -- the run ---------------------------------------------------------------------------
    def run(self, items, max_commits=0, progress_every=25):
        started = now()
        run_id = iso(started).replace(":", "").replace("-", "") + "-" + uuid.uuid4().hex[:6]
        self.run_id = run_id
        self.store.write_run(run_id, {"id": run_id, "status": "running", "started_at": iso(started), "ai": self.ai, "network": self.network,
                                      "providers": [p.name for p in self.providers], "selected": len(items), "manifest": self.manifest["digest"]})
        self.store.event("run-start", run=run_id, selected=len(items), ai=self.ai, network=self.network)
        inflight = []
        position = 0
        status = "complete"
        try:
            self._mark_stale_runs(run_id)
            live = self._load_live_graph()
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.jobs) as pool:
                done = 0
                while (position < len(items) or inflight) and not self.stop.is_set():
                    while position < len(items) and len(inflight) < self.jobs * 3 and not self._limit_reached(max_commits):
                        item = items[position]
                        position += 1
                        self.bump("started")
                        inflight.append((item, pool.submit(self._work, item)))
                    if not inflight:
                        break
                    item, future = inflight.pop(0)
                    try:
                        rec = future.result()
                    except FatalStateError:
                        raise
                    except BudgetExhausted as exc:
                        self._halt(exc.reason)
                        continue
                    except Exception as exc:  # defensive: _work already isolates, but never let one commit end the run
                        self._record_worker_crash(item, exc)
                        continue
                    if rec is None:
                        continue
                    live = self._integrate(item, rec, live)
                    done += 1
                    if progress_every and done % progress_every == 0:
                        self.out(f"  {done}/{len(items)} commits processed")
                        self._persist_graph(live)
                    if self.stop_reason and self.stop_reason.startswith("budget"):
                        break
                for item, future in inflight:
                    future.cancel()
            if self.stop.is_set():
                status = "interrupted"
            elif self.stop_reason:
                status = "budget_exhausted"
            elif position < len(items):
                status = "trial_limit"
            self._finish_graph()
        except FatalStateError as exc:
            status = "fatal"
            self.store.event("fatal", run=run_id, message=str(exc))
            self._finish(run_id, started, status, str(exc))
            raise
        report = coverage_report(self.store, self.manifest)
        if status == "complete" and not self._requested_complete(report):
            status = "incomplete"
        self._finish(run_id, started, status)
        return status, report

    def _requested_complete(self, report):
        """Complete for what THIS run asked for: a deliberate inspection-only run is not a failure, but any
        failure, partial result or pending work in a requested stage (or incomplete history) is."""
        ok = report["inspection_complete"] and report["manifest"]["history_complete"]
        if self.network:
            ok = ok and report["external_complete"]
        if self.ai:
            ok = ok and report["analysis_complete"] and report["linking_complete"]
        return bool(ok)

    def _limit_reached(self, max_commits):
        return bool(self.stop_reason) or (max_commits and self.counters["started"] >= max_commits)

    def _halt(self, reason):
        if not self.stop_reason:
            self.stop_reason = "budget: " + reason
            self.store.event("budget-exhausted", run=self.run_id, reason=reason)

    def _finish(self, run_id, started, status, error=None):
        usage = self.caller.budget.usage if self.caller else {}
        self.store.write_run(run_id, {
            "id": run_id, "status": status, "started_at": iso(started), "ended_at": iso(), "ai": self.ai, "network": self.network,
            "providers": [p.name for p in self.providers], "counters": dict(self.counters), "stop_reason": self.stop_reason,
            "ai_calls": self.caller.budget.calls if self.caller else 0, "usage_reported": usage, "error": error,
            "disabled_providers": self.caller.circuits.snapshot() if self.caller else {},
            "forge": dict(self.forge.stats) if self.forge else None, "manifest": self.manifest["digest"]})
        self.store.event("run-end", run=run_id, status=status)
        if self.caller:
            self.store.write_circuits({"providers": self.caller.circuits.persistable(), "forges": self.forge.circuits if self.forge else {}})
        elif self.forge:
            self.store.write_circuits({"providers": {}, "forges": self.forge.circuits})

    def _mark_stale_runs(self, current):
        for run in self.store.runs():
            if run.get("status") == "running" and run["id"] != current:
                self.store.write_run(run["id"], {**run, "status": "crashed_or_killed", "ended_at": None,
                                                 "note": "run record was never finalized; durable per-commit progress was kept"})

    def _record_worker_crash(self, item, exc):
        rec = self.store.commit(item["sha"]) or blank_commit(item)
        record_error(rec, "worker", "internal_error", f"{type(exc).__name__}: {exc}")
        if rec["analysis"]["status"] == "pending" and self.ai:
            rec["analysis"] = {**rec["analysis"], "status": "failed", "error": "internal error"}
        self.store.put_commit(rec)
        self.bump("failed_commits")

    # -- per-commit worker (thread) ---------------------------------------------------------
    def _work(self, item):
        if self.stop.is_set():
            return None
        sha_ = item["sha"]
        rec = self.store.commit(sha_) or blank_commit(item)
        try:
            return self._work_inner(item, rec)
        except (FatalStateError, BudgetExhausted):
            raise
        except Exception as exc:
            record_error(rec, "worker", "internal_error", f"{type(exc).__name__}: {exc}")
            rec["analysis"] = {**rec["analysis"], "status": "failed" if self.ai else rec["analysis"]["status"], "error": f"internal error: {type(exc).__name__}"}
            self.store.put_commit(rec)
            self.bump("failed_commits")
            return rec

    def _work_inner(self, item, rec):
        sha_ = item["sha"]
        need_ai = self.ai and rec["analysis"]["status"] in ("pending", "failed")
        need_ext = self.network and rec["external"]["status"] in ("pending", "not_requested", "failed")
        need_inspect = rec["inspection"]["status"] in ("pending", "failed") or need_ai or need_ext
        if not need_inspect:
            return rec
        info = None
        try:
            info = gitscan.inspect_commit(self.repo, sha_, self.limits)
            rec["inspection"] = self._inspection_summary(info)
            self.bump("inspected")
        except FatalStateError:
            raise
        except Exception as exc:
            rec["inspection"] = {"status": "failed", "error": str(exc)[:300], "at": iso()}
            record_error(rec, "inspection", "git_error", exc)
            if self.ai:
                rec["analysis"] = {"status": "failed", "error": "inspection failed", "class": "inspection_failed"}
            self.store.put_commit(rec)
            self.bump("failed_commits")
            self.out(f"  ! {sha_[:12]} inspection failed: {str(exc)[:100]}")
            return rec
        self.store.put_commit(rec)
        fault("after_inspection")
        diff_text = "\n".join(f["patch"] or "" for f in info["files"] if f.get("patch"))
        added = "\n".join(l[1:] for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++"))[:200000]
        if need_ext:
            try:
                rec["external"] = {**self.forge.collect(sha_, info["message"], added), "at": iso()}
            except FatalStateError:
                raise
            except Exception as exc:
                rec["external"] = {"status": "failed", "references": [], "resources": [], "limitations": [f"unexpected error: {type(exc).__name__}: {exc}"[:300]]}
                record_error(rec, "external", "forge_error", exc)
            self.store.put_commit(rec)
            fault("after_external")
        elif rec["external"]["status"] == "pending":
            rec["external"] = {"status": "not_requested", "references": [], "resources": [], "limitations": []}
            self.store.put_commit(rec)
        if need_ai:
            self._analyse(item, rec, info)
        return rec

    def _inspection_summary(self, info):
        partial = bool(info["diff_truncated"] or info["message_truncated"] or any(f.get("not_shown") for f in info["files"])
                       or info.get("unattributed_patch") or info["files_total"] > len(info["files"]))
        return {"status": "partial" if partial else "inspected", "kind": info["kind"], "parents": len(info["parents"]),
                "files_total": info["files_total"], "files_with_patch": sum(1 for f in info["files"] if f.get("patch")),
                "binary_files": sum(1 for f in info["files"] if f.get("binary")),
                "symlinks": sum(1 for f in info["files"] if f.get("symlink")), "submodules": sum(1 for f in info["files"] if f.get("submodule")),
                "empty": info["empty"], "limitations": info["limitations"], "changed_paths": [f["path"] for f in info["files"]][:200],
                "merged_commit_count": (info["merge"] or {}).get("merged_commit_count"), "at": iso()}

    # -- AI analysis -------------------------------------------------------------------------
    def _analyse(self, item, rec, info):
        sha_ = item["sha"]
        resources = [r for r in (self.forge.load(k) if self.forge else None for k in rec["external"].get("resources", [])) if r]
        context = []
        if self.limits["context_files"]:
            for entry in info["files"]:
                if len(context) >= self.limits["context_files"]:
                    break
                if entry["status"] in "AMR" and entry.get("patch") and entry.get("blob_oid") and not entry["symlink"]:
                    text, truncated = gitscan.file_at(self.repo, entry["blob_oid"], self.limits["context_chars"])
                    if text is not None:
                        context.append((entry["path"], text, truncated))
        items, coverage = analysis.build_evidence(info, resources, self.limits, context)
        bundles = analysis.make_bundles(items, self.limits)
        limitations = list(coverage["limitations"])
        for resource in resources:
            if resource["status"] == "partial":
                limitations.append(f"forge resource {resource['key']} only partially retrieved")
        if len(bundles) > self.limits["max_chunks_per_commit"]:
            limitations.append(f"{len(bundles) - self.limits['max_chunks_per_commit']} of {len(bundles)} evidence bundles not analysed (per-commit limit)")
            bundles = bundles[:self.limits["max_chunks_per_commit"]]
        if coverage["files_without_patch"]:
            shown = [f for f in coverage["files_without_patch"] if f["reason"].startswith("not shown")]
            if shown:
                limitations.append(f"{len(shown)} changed file(s) had no diff text shown to the model (size limit)")
        commit_paths = [f["path"] for f in info["files"]] + [f["old_path"] for f in info["files"] if f.get("old_path")]
        epoch = rec.get("epoch", 0)
        results, calls = [], 0
        for bundle in bundles:
            if self.stop.is_set():
                raise BudgetExhausted("cancelled")
            leafs = self._run_bundle(sha_, bundle, items, commit_paths, epoch)
            results.extend(leafs)
        chunk_ok = [r for r in results if r["status"] == "ok"]
        calls = sum(r.get("calls", 0) for r in results)
        findings, rejected, problems = [], [], []
        seen = {}
        committer_ts = info["committer"].get("timestamp")
        for result in chunk_ok:
            rejected.extend(result.get("rejected", []))
            for raw in result["findings"]:
                finding = analysis.attach_sources(raw, items)
                finding["commit"] = sha_
                finding["id"] = analysis.finding_id(sha_, finding)
                finding["date"] = iso(committer_ts) if committer_ts else None
                subject, _ = redact(info["message"])
                finding["subject"] = subject.strip().splitlines()[0][:200] if subject.strip() else ""
                finding["analysis"] = {"provider": result.get("provider"), "model": result.get("model"), "chunk": result["id"],
                                       "analysis_version": ANALYSIS_VERSION, "prompt_version": PROMPT_VERSION}
                if finding["id"] in seen:
                    known = seen[finding["id"]]
                    have = {(e["ref"], e.get("quote")) for e in known["evidence"]}
                    known["evidence"] += [e for e in finding["evidence"] if (e["ref"], e.get("quote")) not in have]
                else:
                    seen[finding["id"]] = finding
                    findings.append(finding)
        consolidation = None
        if len([r for r in chunk_ok if r["findings"]]) > 1 and len(findings) > 1 and self.ai:
            findings, consolidation = self._consolidate(sha_, findings)
        problems = [p for r in chunk_ok for p in r.get("problems", [])]
        if rejected and not findings:
            limitations.append(f"{len(rejected)} model finding(s) were rejected as unsupported; absence of intent cannot be concluded")
        failed_chunks = [r for r in results if r["status"] != "ok"]
        status = self._analysis_status(bool(findings), len(failed_chunks), len(results), limitations, len(bundles))
        attempt_no = len(self.store.list_nested("attempts", sha_)) + 1
        attempt = {"commit": sha_, "attempt": attempt_no, "analysis_version": ANALYSIS_VERSION, "prompt_version": PROMPT_VERSION,
                   "created_at": iso(), "epoch": epoch, "status": status, "findings": findings, "rejected": rejected[:50],
                   "validation_problems": problems[:30], "chunks": [self._chunk_summary(r) for r in results],
                   "coverage": {k: coverage[k] for k in ("items", "files_total", "files_with_patch", "files_without_patch", "redactions")},
                   "limitations": limitations, "consolidation": consolidation}
        self.store.put_nested("attempts", sha_, f"{attempt_no:06d}", attempt)
        fault("after_attempt_write")
        rec["analysis"] = {"status": status, "attempt": attempt_no, "findings": len(findings), "rejected": len(rejected),
                           "chunks": [self._chunk_summary(r) for r in results], "limitations": limitations,
                           "providers_used": sorted({r.get("provider") for r in chunk_ok if r.get("provider")}),
                           "calls": calls, "analysis_version": ANALYSIS_VERSION, "at": iso()}
        if failed_chunks:
            record_error(rec, "analysis", failed_chunks[0].get("error", {}).get("class", "provider_failure"),
                         failed_chunks[0].get("error", {}).get("message", "chunk failed"))
        rec["link"] = {"status": "pending"} if findings else {"status": "not_applicable"}
        fault("before_commit_record")
        self.store.put_commit(rec)
        if status in TERMINAL_ANALYSIS or status == "partial":
            self.bump("analyzed")
        if status == "failed":
            self.bump("failed_commits")
            self.out(f"  ! {sha_[:12]} analysis failed: {failed_chunks[0].get('error', {}).get('message', '')[:100] if failed_chunks else ''}")

    @staticmethod
    def _analysis_status(has_findings, failed, total, limitations, planned):
        if total == 0 or failed == total:
            return "failed"
        incomplete = bool(failed or limitations)
        if has_findings:
            return "partial" if incomplete else "analyzed"
        return "partial" if incomplete else "no_intent_found"

    @staticmethod
    def _chunk_summary(result):
        return {k: result.get(k) for k in ("id", "items", "status", "provider", "model", "calls", "split_from", "reused")
                } | ({"error": result["error"]} if result.get("error") else {})

    def _run_bundle(self, sha_, bundle, items, commit_paths, epoch, split_from=None):
        """Analyse one bundle; on a context-window error split it and analyse the halves."""
        bid = analysis.bundle_id(bundle)
        key = f"{bid}-{epoch}"
        cached = self.store.get_nested("chunks", sha_, key)
        if cached and cached.get("status") == "ok" and cached.get("analysis_version") == ANALYSIS_VERSION:
            self.bump("chunks_reused")
            return [{**cached, "reused": True}]
        refs = [i["ref"] for i in bundle]
        prompt = analysis.extraction_prompt(bundle)
        parse = analysis.make_extraction_parser(bundle, commit_paths)
        result = {"id": bid, "key": key, "items": refs, "analysis_version": ANALYSIS_VERSION, "split_from": split_from}
        try:
            outcome = self.caller.complete(prompt, parse)
            value = outcome["value"]
            result.update(status="ok", findings=value["findings"], rejected=value["rejected"], problems=outcome["problems"],
                          provider=outcome["provider"], model=outcome["model"], usage=outcome["usage"], calls=outcome.get("calls", 0))
        except OversizeError as exc:
            parts = analysis.split_bundle(bundle)
            calls = getattr(exc, "calls", 1)
            if not parts:
                result.update(status="failed", calls=calls, error={"class": "context_window", "message": "input exceeds the context window and cannot be split further"})
                self.store.put_nested("chunks", sha_, key, result)
                return [result]
            self.store.event("bundle-split", commit=sha_, bundle=bid, parts=len(parts))
            out = []
            for part in parts:
                out.extend(self._run_bundle(sha_, part, items, commit_paths, epoch, split_from=bid))
            out[0] = {**out[0], "calls": out[0].get("calls", 0) + calls}
            return out
        except CallFailed as exc:
            last = next((e for e in reversed(exc.log) if e.get("event") in ("failure", "invalid-output", "skipped")), {})
            result.update(status="failed", calls=exc.calls, error={"class": last.get("class") or last.get("kind") or last.get("event") or "provider_failure",
                                                                 "message": str(last.get("message") or last.get("problems") or last.get("reason") or "all providers failed")[:300],
                                                                 "log": exc.log[-6:]})
            self.store.put_nested("chunks", sha_, key, result)
            return [result]
        self.store.put_nested("chunks", sha_, key, result)
        return [result]

    def _consolidate(self, sha_, findings):
        for n, f in enumerate(findings):
            f.setdefault("id", f"F-{n}")
        parse = analysis.make_consolidation_parser(findings)
        items = analysis.consolidation_items(findings)
        prompt = analysis.render_prompt(items, analysis.CONSOLIDATE_RULES, analysis.CONSOLIDATE_SCHEMA)
        try:
            outcome = self.caller.complete(prompt, parse)
        except (CallFailed, OversizeError) as exc:
            return findings, {"status": "failed", "note": "kept the union of per-chunk findings", "error": str(exc)[:200]}
        merged = analysis.apply_consolidation(findings, outcome["value"])
        for f in merged:
            if f.get("id") is None:
                f["id"] = analysis.finding_id(sha_, f)
        before = len(findings)
        # Preserve the per-chunk originals through `merged_from`; nothing is dropped.
        return merged, {"status": "ok", "findings_before": before, "findings_after": len(merged), "provider": outcome["provider"],
                        "model": outcome["model"], "calls": outcome.get("calls", 1)}

    # -- linking and graph (ordered) -------------------------------------------------------------
    def _entries(self, upto=None):
        """Graph entries from durable records, in manifest order."""
        entries = []
        for item in self.manifest["commits"]:
            if upto is not None and item["index"] >= upto:
                break
            entries.append(self._entry(item))
        return entries

    def _entry(self, item):
        rec = self.store.commit(item["sha"])
        findings, decisions = [], []
        if rec and rec["analysis"].get("attempt") and rec["analysis"]["status"] in ("analyzed", "partial"):
            attempt = self.store.get_nested("attempts", item["sha"], f"{rec['analysis']['attempt']:06d}")
            findings = attempt["findings"] if attempt else []
            link = self.store.get("links", item["sha"])
            if link and link.get("attempt") == rec["analysis"]["attempt"]:
                decisions = link.get("decisions", [])
        return {"sha": item["sha"], "index": item["index"], "date": date_of(item), "findings": findings, "decisions": decisions}

    def _load_live_graph(self):
        """Live graph = fold of every commit already integrated or without pending link work."""
        entries = []
        for item in self.manifest["commits"]:
            rec = self.store.commit(item["sha"])
            if rec and rec["link"]["status"] in ("linked", "no_candidates") or (rec and rec["analysis"]["status"] in ("analyzed", "partial") and rec["link"]["status"] in ("failed", "stale")):
                entries.append(self._entry(item))
        graph = graph_mod.fold(entries)
        graph.cursor = max((e["index"] for e in entries), default=-1)
        return graph

    def _integrate(self, item, rec, live):
        if rec["link"]["status"] not in ("pending", "stale", "failed") or rec["analysis"]["status"] not in ("analyzed", "partial"):
            return live
        if not self.ai:
            return live
        out_of_order = item["index"] <= getattr(live, "cursor", -1)
        graph = graph_mod.fold(self._entries(upto=item["index"])) if out_of_order else live
        entry = self._entry(item)
        findings = entry["findings"]
        info_msg = self._message(item["sha"])
        reverted = gitscan.reverted_commits(info_msg)
        decisions = graph_mod.revert_decisions(graph, findings, reverted, info_msg)
        link_state = {"status": "linked", "decisions": 0}
        record = {"commit": item["sha"], "attempt": rec["analysis"]["attempt"], "decisions": decisions, "created_at": iso()}
        paths = rec["inspection"].get("changed_paths", [])
        candidates = graph_mod.candidate_nodes(graph, findings, paths, set(reverted))
        if candidates and findings:
            items = graph_mod.link_items(findings, candidates, info_msg)
            try:
                outcome = self.caller.complete(graph_mod.link_prompt(items), graph_mod.make_link_parser(items, findings, candidates))
                have = {(d["finding"], d["target"], d["relation"]) for d in decisions}
                decisions += [d for d in outcome["value"] if (d["finding"], d["target"], d["relation"]) not in have]
                record.update(decisions=decisions, provider=outcome["provider"], model=outcome["model"], problems=outcome["problems"][:10],
                              candidates=[c["id"] for c in candidates])
            except BudgetExhausted as exc:
                self._halt(exc.reason)
                return live
            except (CallFailed, OversizeError) as exc:
                record.update(status="failed", error=str(exc)[:200])
                link_state = {"status": "failed", "error": str(exc)[:200]}
                record_error(rec, "link", "provider_failure", exc)
        elif not candidates:
            link_state = {"status": "no_candidates", "decisions": len(decisions)}
        record["status"] = link_state["status"]
        link_state["decisions"] = len(decisions)
        self.store.put("links", item["sha"], record)
        fault("after_link_write")
        rec["link"] = {**link_state, "at": iso()}
        self.store.put_commit(rec)
        if out_of_order:
            live = self._load_live_graph()
        else:
            live.apply(entry | {"decisions": decisions}, findings, decisions)
            live.cursor = max(getattr(live, "cursor", -1), item["index"])
        return live

    def _message(self, sha_):
        try:
            return gitscan.parse_commit_object(self.repo.run("cat-file", "commit", sha_))["message"]
        except Exception:
            return ""

    def _persist_graph(self, live):
        try:
            graph_mod.write_graph(self.store, graph_mod.fold(self._entries()))
        except FatalStateError:
            raise

    def _finish_graph(self):
        graph_mod.write_graph(self.store, graph_mod.fold(self._entries()))


# ---------------------------------------------------------------------------------------------
# Reports


def coverage_report(store, manifest=None, list_limit=200):
    manifest = manifest or store.manifest()
    keys = {"inspection": ("inspected", "partial", "failed", "pending"),
            "analysis": ("analyzed", "no_intent_found", "partial", "failed", "skipped", "pending"),
            "external": ("complete", "none_found", "partial", "failed", "not_requested", "pending"),
            "link": ("linked", "no_candidates", "not_applicable", "failed", "stale", "pending")}
    counts = {stage: {k: 0 for k in names} for stage, names in keys.items()}
    incomplete, total, kinds = [], 0, {"root": 0, "merge": 0, "normal": 0, "empty": 0}
    for item in manifest["commits"]:
        total += 1
        rec = store.commit(item["sha"]) or blank_commit(item)
        for stage in keys:
            status = rec[stage]["status"]
            counts[stage][status] = counts[stage].get(status, 0) + 1
        if rec["inspection"].get("kind"):
            kinds[rec["inspection"]["kind"]] = kinds.get(rec["inspection"]["kind"], 0) + 1
        if rec["inspection"].get("empty"):
            kinds["empty"] += 1
        bad = [(stage, rec[stage]["status"]) for stage in ("inspection", "analysis", "external", "link")
               if rec[stage]["status"] in ("failed", "partial", "pending", "stale") and not (stage == "link" and rec["analysis"]["status"] not in ("analyzed", "partial"))]
        for stage, status in bad:
            reason = (rec[stage].get("error") or "; ".join(rec[stage].get("limitations", [])[:2]) or
                      (rec["errors"][-1]["message"] if rec["errors"] else ""))
            incomplete.append({"sha": item["sha"], "index": item["index"], "stage": stage, "status": status, "reason": str(reason)[:200]})
    graph = graph_mod.load_graph(store)
    findings = [f for n in graph.nodes.values() for f in n["findings_data"] + n["supporting_data"]]
    discoveries = {"nodes": len(graph.nodes), "findings": len(findings),
                   "explicit": sum(1 for f in findings if f["basis"] == "explicit"), "inferred": sum(1 for f in findings if f["basis"] == "inferred"),
                   "ended_in_history": sum(1 for n in graph.nodes.values() if n["lifecycle"] != graph_mod.ACTIVE),
                   "contested": sum(1 for n in graph.nodes.values() if n["contested_by"]),
                   "dangling_links": len(graph.dangling), "merge_gate_rejections": len(graph.gate_rejections)}
    ins, ana, ext = counts["inspection"], counts["analysis"], counts["external"]
    inspection_complete = ins["inspected"] == total
    analysis_complete = ana["analyzed"] + ana["no_intent_found"] == total
    external_complete = ext["partial"] + ext["failed"] + ext["pending"] == 0
    link = counts["link"]
    linking_complete = link["failed"] + link["stale"] + link["pending"] == 0
    coverage = manifest["coverage"]
    return {"commits_in_scope": total, "manifest": {"digest": manifest["digest"], "generations": len(manifest["generations"]),
                                                    "history_complete": coverage["complete"], "notes": coverage["notes"]},
            "commit_kinds": kinds, "inspection": ins, "ai_analysis": ana, "external_evidence": ext, "linking": counts["link"],
            "discoveries": discoveries,
            "complete": bool(inspection_complete and analysis_complete and external_complete and linking_complete and coverage["complete"]),
            "inspection_complete": inspection_complete, "analysis_complete": analysis_complete, "external_complete": external_complete,
            "linking_complete": linking_complete,
            "incomplete_items": incomplete[:list_limit], "incomplete_total": len(incomplete),
            "label": "Counts describe coverage of evidence gathering and AI analysis, not the truth of any discovery."}
