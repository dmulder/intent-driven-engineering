"""Durable state: crashes before/after checkpoint writes, resume, locking, versioning, reanalysis, integrity."""
import json
import os
from pathlib import Path
import unittest

from trueup_support import TrueUpTest, provider_spec
from intentlib.trueup import util
from intentlib.trueup.util import SimulatedCrash


class Crash:
    """Context manager: raise SimulatedCrash the Nth time a named fault point is reached."""

    def __init__(self, point, nth=1, startswith=False):
        self.point, self.nth, self.startswith, self.hits = point, nth, startswith, 0

    def __enter__(self):
        def hook(name):
            if self.hits >= self.nth:
                raise SimulatedCrash(name)  # the "process" is dead: no later write may happen from other threads
            if name == self.point or (self.startswith and name.startswith(self.point)):
                self.hits += 1
                if self.hits == self.nth:
                    raise SimulatedCrash(name)
        util.FAULT_HOOK = hook
        return self

    def __exit__(self, *exc):
        util.FAULT_HOOK = None
        return False


def snapshot_graph(test):
    graph = test.graph()
    return {n: (node["lifecycle"], [t["commit"] for t in node["timeline"]], sorted(node["paths"])) for n, node in graph.nodes.items()}


class CrashAndResume(TrueUpTest):
    def prepare(self):
        c1, c2, c3, c4 = self.small_history()
        self.init(providers={"fake": provider_spec()})
        return c1, c2, c3, c4

    def test_crash_at_every_checkpoint_then_resume_matches_uninterrupted_run(self):
        # Uninterrupted reference in a second state directory.
        self.small_history()
        reference_state = self.tmp / "reference"
        self.init(providers={"fake": provider_spec()}, extra_args=["--state-dir", str(reference_state)], state=False)
        self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", "--state-dir", str(reference_state), state=False)
        from intentlib.trueup.store import Store
        from intentlib.trueup import graph as graph_mod
        ref_graph = graph_mod.load_graph(Store.open(reference_state))
        expected = {n: (node["lifecycle"], [t["commit"] for t in node["timeline"]]) for n, node in ref_graph.nodes.items()}
        self.assertTrue(expected)

        points = [("after_inspection", 2, False), ("after_attempt_write", 1, False), ("before_commit_record", 3, False),
                  ("after_link_write", 2, False), ("atomic_write:before_rename:", 5, True)]
        for point, nth, prefix in points:
            with self.subTest(point=point):
                state = self.tmp / ("crash-" + point.replace(":", "_"))
                self.tuj("init", "--state-dir", str(state), state=False)
                args = ["run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", "--state-dir", str(state)]
                # Provider config lives in the store config; copy it so this state can call the fake CLI.
                cfg = json.loads((state / "config.json").read_text())
                cfg["providers"] = {"fake": provider_spec()}
                (state / "config.json").write_text(json.dumps(cfg))
                with Crash(point, nth, prefix) as crash:
                    with self.assertRaises(SimulatedCrash):
                        self.tu(*args, state=False)
                self.assertGreaterEqual(crash.hits, nth)
                for path in state.rglob("*.json"):
                    json.loads(path.read_text())  # no torn JSON record is ever visible
                status = self.tuj("status", "--state-dir", str(state), state=False)
                self.assertFalse(status["coverage"]["complete"])
                self.assertTrue(any("never finished" in w for w in status["warnings"]), status["warnings"])
                resumed = self.tuj(*args, state=False)
                self.assertEqual(resumed["status"], "complete", resumed["coverage"])
                self.assertFalse(list(state.rglob(".tmp-*")), "crash leftovers are cleaned on resume")
                graph = graph_mod.load_graph(Store.open(state))
                got = {n: (node["lifecycle"], [t["commit"] for t in node["timeline"]]) for n, node in graph.nodes.items()}
                self.assertEqual(got, expected, "resumed run converges to the uninterrupted result")
                runs = Store.open(state).runs()
                self.assertIn("crashed_or_killed", [r["status"] for r in runs])

    def test_crash_after_attempt_write_keeps_attempt_and_reanalysis_appends(self):
        c1, *_ = self.prepare()
        with Crash("after_attempt_write", 1):
            with self.assertRaises(SimulatedCrash):
                self.tu("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        store = self.store()
        self.assertEqual(store.list_nested("attempts", c1), ["000001"])
        rec = store.commit(c1)
        self.assertIn(rec["analysis"]["status"], ("pending",), "the commit is not marked analysed until the record is durable")
        first_bytes = (self.state / "attempts" / c1[:2] / c1 / "000001.json").read_bytes()
        calls_before = len(self.calls())
        self.tu("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        self.assertEqual((self.state / "attempts" / c1[:2] / c1 / "000001.json").read_bytes(), first_bytes, "earlier attempts are never overwritten")
        self.assertEqual(self.store().list_nested("attempts", c1), ["000001", "000002"])
        self.assertEqual(self.store().commit(c1)["analysis"]["attempt"], 2)

    def test_chunk_results_survive_a_crash_and_are_reused(self):
        c1, *_ = self.prepare()
        with Crash("after_attempt_write", 1):
            with self.assertRaises(SimulatedCrash):
                self.tu("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", "--max-commits", "1")
        before = len(self.calls())
        result = self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", "--max-commits", "1", ok=3)
        self.assertEqual(result["counters"]["chunks_reused"], 1)
        self.assertEqual(len(self.calls()), before, "the already-answered chunk is not sent to the model again")

    def test_crash_before_checkpoint_loses_only_the_in_flight_commit(self):
        self.prepare()
        with Crash("after_inspection", 3):
            with self.assertRaises(SimulatedCrash):
                self.tu("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        cov = self.tuj("status")["coverage"]
        self.assertEqual(cov["ai_analysis"]["analyzed"], 2)
        self.assertEqual(cov["inspection"]["inspected"] + cov["inspection"]["pending"], 4)

    def test_interrupt_flag_stops_cleanly_between_commits(self):
        self.prepare()
        import threading
        from intentlib.trueup import cli as tcli
        stop_after = {"n": 0}
        original = util.fault

        def hook(name):
            if name == "after_link_write":
                stop_after["n"] += 1
                if stop_after["n"] == 1:
                    os.kill(os.getpid(), 2)  # SIGINT delivered to the handler installed by `run`
        util.FAULT_HOOK = hook
        try:
            code, out, err = self.tu("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", "--json", ok=130)
        finally:
            util.FAULT_HOOK = None
        data = json.loads(out)
        self.assertEqual(data["status"], "interrupted")
        self.assertFalse(data["complete"])
        self.assertLess(data["coverage"]["ai_analysis"]["analyzed"], 4)
        self.assertEqual(self.store().runs()[-1]["status"], "interrupted")
        resumed = self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        self.assertEqual(resumed["status"], "complete")

    def test_rerun_after_completion_is_idempotent(self):
        self.prepare()
        self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        calls = len(self.calls())
        again = self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        self.assertEqual(again["status"], "complete")
        self.assertEqual(len(self.calls()), calls)

    def test_parallel_jobs_produce_the_same_graph_as_serial(self):
        for i in range(8):
            self.repo.commit(f"decision {i}" + (" adjusts the earlier one" if i % 3 == 2 else ""), {"f.py": f"v{i}\n"})
        self.init(providers={"fake": provider_spec()})
        self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", "--jobs", "4")
        parallel = snapshot_graph(self)
        import shutil
        shutil.rmtree(self.state)
        self.init(providers={"fake": provider_spec()})
        self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", "--jobs", "1")
        self.assertEqual(snapshot_graph(self), parallel)


class LocksVersionsAndIntegrity(TrueUpTest):
    def prepare(self):
        self.small_history()
        self.init(providers={"fake": provider_spec()})

    def test_single_writer_lock(self):
        self.prepare()
        holder = self.store()
        holder.acquire()
        try:
            code, out, err = self.tu("run", "--no-ai", ok=1)
            self.assertIn("another true-up process", err)
            self.tuj("status")  # readers are not blocked
        finally:
            holder.release()
        self.tu("run", "--no-ai")

    def test_incompatible_analysis_version_is_detected_on_resume(self):
        self.prepare()
        config = json.loads((self.state / "config.json").read_text())
        config["analysis_version"] = "0"
        (self.state / "config.json").write_text(json.dumps(config))
        code, out, err = self.tu("run", "--no-ai", ok=1)
        self.assertIn("--accept-version-change", err)
        self.tu("run", "--no-ai", "--accept-version-change")

    def test_incompatible_store_format_is_refused(self):
        self.prepare()
        meta = json.loads((self.state / "meta.json").read_text())
        meta["format"] = 999
        (self.state / "meta.json").write_text(json.dumps(meta))
        code, out, err = self.tu("status", ok=1)
        self.assertIn("not supported", err)

    def test_missing_manifest_is_reported_not_guessed(self):
        self.prepare()
        for p in (self.state / "manifests").glob("*.json"):
            p.unlink()
        code, out, err = self.tu("run", "--no-ai", ok=1)
        self.assertIn("no manifest", err)

    def test_corrupt_record_does_not_hide_other_commits_and_is_reported_by_verify(self):
        self.prepare()
        self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        c = self.store().manifest()["commits"][1]["sha"]
        attempt = self.state / "attempts" / c[:2] / c / "000001.json"
        attempt.unlink()
        report = self.tuj("verify", ok=False)
        self.assertFalse(report["ok"])
        self.assertEqual(report["issues"][0]["sha"], c)

    def test_unwritable_state_is_fatal_but_prior_work_is_preserved(self):
        if os.geteuid() == 0:
            self.skipTest("permissions are not enforced for root")
        self.prepare()
        self.tuj("run", "--no-ai", "--max-commits", "2", ok=3)
        os.chmod(self.state / "commits", 0o500)
        try:
            for sub in (self.state / "commits").iterdir():
                os.chmod(sub, 0o500)
            code, out, err = self.tu("run", "--no-ai", ok=1)
            self.assertIn("fatal", err)
        finally:
            for sub in (self.state / "commits").iterdir():
                os.chmod(sub, 0o700)
            os.chmod(self.state / "commits", 0o700)
        self.assertEqual(self.tuj("status")["coverage"]["inspection"]["inspected"], 2)

    def test_torn_final_audit_line_is_tolerated(self):
        self.prepare()
        self.tuj("run", "--no-ai")
        with open(self.state / "events.jsonl", "ab") as stream:
            stream.write(b'{"kind": "run-st')
        self.tuj("status")
        self.assertEqual(self.tuj("verify", ok=False)["issues"], [])

    def test_verify_repair_removes_only_temp_leftovers(self):
        self.prepare()
        self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        leftover = self.state / "commits" / ".tmp-orphan"
        leftover.write_text("partial")
        report = self.tuj("verify", "--repair", ok=False)
        self.assertEqual(report["removed_temp_files"], 1)
        self.assertFalse(leftover.exists())
        self.assertTrue(report["graph_rebuilt"])

    def test_state_never_contains_forge_tokens(self):
        os.environ["GITHUB_TOKEN"] = "ghp_SECRETSECRETSECRETSECRET1234567890"
        self.prepare()
        self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        text = "".join(p.read_text(errors="replace") for p in self.state.rglob("*") if p.is_file())
        self.assertNotIn("SECRETSECRET", text)

    def test_data_sharing_requires_explicit_acknowledgement(self):
        self.prepare()
        code, out, err = self.tu("run", "--provider", "fake", ok=1)
        self.assertIn("--acknowledge-data-sharing", err)
        self.assertEqual(self.calls(), [])
        code, out, err = self.tu("run", ok=1)
        self.assertIn("no AI provider selected", err)

    def test_local_provider_does_not_need_acknowledgement(self):
        self.small_history()
        self.init(providers={"localmodel": provider_spec(local=True)})
        self.tuj("run", "--provider", "localmodel", "--backoff-base", "0")
        self.assertTrue(self.calls())

    def test_no_ai_flag_never_runs_a_provider(self):
        self.prepare()
        self.tuj("run", "--no-ai")
        self.assertEqual(self.calls(), [])

    def test_missing_provider_executable_is_diagnosed_before_any_work(self):
        self.small_history()
        self.init(providers={"ghost": {"bin": "/nonexistent/ghost", "invocations": [{"argv": ["{bin}"], "stdin": True}]}})
        code, out, err = self.tu("run", "--provider", "ghost", "--acknowledge-data-sharing", ok=1)
        self.assertIn("none of the configured AI providers is installed", err)
        self.assertEqual(self.tuj("status")["coverage"]["inspection"]["pending"], 4)


class Reanalysis(TrueUpTest):
    def test_reanalyze_appends_attempt_without_silent_overwrite(self):
        c1, c2, c3, c4 = self.small_history()
        self.init(providers={"fake": provider_spec()})
        self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        store = self.store()
        first = (self.state / "attempts" / c2[:2] / c2 / "000001.json").read_bytes()
        out = self.tuj("reanalyze", c2, "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        self.assertEqual(out["status"], "complete")
        self.assertEqual(self.store().list_nested("attempts", c2), ["000001", "000002"])
        self.assertEqual((self.state / "attempts" / c2[:2] / c2 / "000001.json").read_bytes(), first)
        rec = self.store().commit(c2)
        self.assertEqual((rec["epoch"], rec["analysis"]["attempt"]), (1, 2))
        events = [e["kind"] for e in self.store().events()]
        self.assertIn("reanalyze", events)
        shown = self.tuj("show", c2[:10])
        self.assertEqual(len(shown["attempts"]), 2)

    def test_reanalyze_unknown_commit_is_rejected(self):
        self.small_history()
        self.init(providers={"fake": provider_spec()})
        code, out, err = self.tu("reanalyze", "f" * 40, "--provider", "fake", "--acknowledge-data-sharing", ok=1)
        self.assertIn("not in the frozen manifest", err)


class StatusSeparatesCoverage(TrueUpTest):
    def test_status_reports_stages_separately(self):
        self.repo.commit("good one", {"a": "1"})
        self.repo.commit("bad one [ai-fail]", {"b": "1"})
        self.repo.commit("good two", {"c": "1"})
        self.init(providers={"fake": provider_spec()})
        self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", ok=4)
        status = self.tuj("status")
        cov = status["coverage"]
        self.assertEqual(cov["inspection"]["inspected"], 3)
        self.assertEqual(cov["ai_analysis"]["analyzed"], 2)
        self.assertEqual(cov["ai_analysis"]["failed"], 1)
        self.assertEqual(cov["external_evidence"]["not_requested"], 3)
        self.assertEqual(cov["discoveries"]["nodes"], 2)
        self.assertTrue(cov["inspection_complete"])
        self.assertFalse(cov["analysis_complete"])
        self.assertFalse(cov["complete"])
        text = self.tu("status")[1]
        self.assertIn("COMPLETE: False", text)
        self.assertIn("NON-AUTHORITATIVE", text)
        report = self.tuj("report")
        self.assertEqual(report["coverage"]["incomplete_total"], 1)
        self.assertEqual(report["usage_across_runs"]["ai_calls"] > 0, True)


if __name__ == "__main__":
    unittest.main()
