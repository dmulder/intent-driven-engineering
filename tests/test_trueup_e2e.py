"""End to end: retrofit a repository that never used intentctl, survive AI and forge failures, resume,
query a file, adopt intentctl, review and selectively promote through the canonical workflow."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from trueup_support import GIT_ENV, MockForge, PROJECT, TrueUpTest, provider_spec


class EndToEnd(TrueUpTest):
    def build_history(self, mock):
        r = self.repo
        mock.github_issue("acme/widgets", 7, "Token comparison", "Timing attacks on token checks.",
                          ["We considered bcrypt but rejected it: too slow for per-request checks."], pull=True)
        mock.github_issue("acme/widgets", 8, "Docs ticket", "Document the token flow.", [])
        mock.queue("/repos/acme/widgets/issues/8", *[(500, {}, {"message": "upstream hiccup"})] * 3)
        self.c1 = r.commit("Add auth check\n\nKeep the first version simple.", {"auth.py": "def auth(t):\n    return t == 'x'\n"})
        self.c2 = r.commit("Use constant-time comparison (#7)\n\nPrevents timing attacks; adjusts the earlier check. [ai-flaky]",
                           {"auth.py": "import hmac\n\ndef auth(t):\n    return hmac.compare_digest(t, 'x')\n"})
        self.c3 = r.commit("Mention ticket #8 in docs", {"docs.md": "see #8\n"})
        self.c4 = r.commit("Revert constant-time comparison\n\nThis reverts commit " + self.c2 + "; reverts the timing fix for old clients.",
                           {"auth.py": "def auth(t):\n    return t == 'x'\n"})
        self.c5 = r.commit("Add logging module", {"log.py": "import logging\n"})

    def adopt_intentctl(self):
        r = self.repo
        shutil.copytree(PROJECT / "tools", r.path / "tools", ignore=shutil.ignore_patterns("__pycache__"))
        r.write(".gitignore", "__pycache__/\n.intent/drafts/\n.intent/cache/\n")
        r.git("add", ".")
        self.ctl("init")
        r.git("add", ".")
        self.ctl("baseline")
        r.git("add", ".")
        r.git("commit", "-qm", "adopt intentctl")

    def ctl(self, *args, ok=True, state=None):
        env = {**os.environ, **GIT_ENV, "INTENTCTL_TRUEUP_STATE_DIR": str(state or self.state)}
        result = subprocess.run([sys.executable, "tools/intentctl.py", *args, "--json"], cwd=self.repo.path, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if ok and result.returncode:
            self.fail(f"{args}: {result.stdout.decode()} {result.stderr.decode()}")
        try:
            return json.loads(result.stdout)
        except ValueError:
            return {"_raw": result.stdout.decode(), "_err": result.stderr.decode(), "_rc": result.returncode}

    def intent_bytes(self):
        root = self.repo.path / ".intent"
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and "drafts" not in p.parts and "cache" not in p.parts}

    def test_full_retrofit_story(self):
        os.environ["FAKE_AI_FLAKY"] = "3"
        mock = MockForge()
        self.addCleanup(mock.close)
        self.build_history(mock)
        before = self.repo.state_fingerprint()
        self.init(providers={"fake": provider_spec()}, extra_args=["--forge", mock.github_spec("acme/widgets"), "--network", "forge"],
                  config={"forge": {"limits": {"backoff_base": 0}}})

        # Run 1: a flaky model request and a flaky forge request fail; every other commit still succeeds.
        first = self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", ok=4)
        self.assertEqual(first["status"], "incomplete")
        self.assertFalse(first["complete"])
        cov = first["coverage"]
        self.assertEqual(cov["commits_in_scope"], 5)
        self.assertEqual(cov["ai_analysis"]["failed"], 1, "the flaky commit failed")
        self.assertGreaterEqual(cov["ai_analysis"]["analyzed"], 4, "later commits were analysed regardless")
        self.assertEqual(cov["external_evidence"]["failed"], 1, "the forge outage on #8 was recorded")
        self.assertEqual(cov["external_evidence"]["complete"], 1, "PR #7 evidence was collected")
        failures = self.tuj("failures")
        self.assertEqual({(i["sha"], i["stage"]) for i in failures["items"]}, {(self.c2, "analysis"), (self.c3, "external")})
        self.assertEqual(self.repo.state_fingerprint(), before, "the target repository was not modified")

        # Retry only the failed items. The model is still flaky: this time extraction succeeds but linking fails.
        calls_before = len(self.calls())
        second = self.tuj("retry", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", ok=4)
        self.assertTrue(second["coverage"]["analysis_complete"] and second["coverage"]["external_complete"])
        self.assertEqual([(i["sha"], i["stage"]) for i in second["coverage"]["incomplete_items"]], [(self.c2, "link")])
        third = self.tuj("retry", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        self.assertEqual(third["status"], "complete", third["coverage"]["incomplete_items"])
        self.assertTrue(third["coverage"]["complete"])
        self.assertLessEqual(len(self.calls()) - calls_before, 8, "retries touched only the failed commit")

        # The decision timeline: introduced, refined with PR evidence, then reversed.
        graph = self.graph()
        by_title = {n["title"]: n for n in graph.nodes.values()}
        auth = by_title["Add auth check"]
        self.assertEqual(auth["lifecycle"], "reversed")
        self.assertEqual([t["commit"] for t in auth["timeline"]], [self.c1, self.c2])
        self.assertEqual(auth["ended_by"][0]["commit"], self.c4)
        self.assertIn("github:127.0.0.1/acme/widgets#7", auth["resource_keys"])

        # Query a file: retrospective claims with evidence, labeled non-authoritative.
        section = self.tuj("query", "auth.py:2", "--detail")
        self.assertEqual(section["authority"], "non-authoritative")
        titles = [d["title"] for d in section["discoveries"]]
        self.assertIn("Add auth check", titles)
        discovery = next(d for d in section["discoveries"] if d["title"] == "Add auth check")
        evidence_refs = {e["ref"] for f in discovery["findings"] for e in f["evidence"]}
        self.assertIn("C0", evidence_refs)
        links = {e["link"] for f in discovery["findings"] for e in f["evidence"] if e["link"]}
        self.assertTrue(any(l.startswith("git:commit/") for l in links))
        self.assertEqual(discovery["historical_lifecycle"], "reversed")
        self.assertEqual(discovery["present_applicability"]["status"], "ended-in-history")

        # Adopt intentctl AFTER the true-up; canonical records start from the unknown-provenance baseline.
        self.adopt_intentctl()
        canonical_before = self.intent_bytes()
        explain = self.ctl("explain", "auth.py:1")
        self.assertEqual(explain["applicable"], [], "nothing canonical governs this code yet")
        self.assertIn("retrospective", explain)
        self.assertEqual(explain["retrospective"]["authority"], "non-authoritative")
        self.assertEqual(self.ctl("explain", "auth.py:1", "--no-retrospective").get("retrospective"), None)
        history = self.ctl("history", "auth.py:1")
        self.assertEqual(history["baseline_provenance"], "unknown")
        self.assertEqual(history["intent_revisions"], [])
        self.assertTrue(history["retrospective"]["discoveries"])

        # Review and selective promotion.
        node_live = by_title["Add logging module"]
        node_ended = auth
        _, _, err = self.tu("promote", node_ended["id"], "--confirm", "0" * 64, ok=1)
        self.assertIn("approve", err, "promotion requires an explicit review first")
        self.tuj("review", node_ended["id"], "--decision", "reject", "--reviewer", "alice", "--note", "reverted anyway")
        _, _, err = self.tu("promote", node_ended["id"], "--confirm", "0" * 64, ok=1)
        self.assertIn("approve", err)

        self.tuj("review", node_live["id"], "--decision", "approve", "--reviewer", "alice", "--note", "matches my memory")
        shown = self.tuj("show", node_live["id"])
        digest = shown["review"]["node_hash"]
        self.tu("promote", node_live["id"], "--confirm", "f" * 64, ok=1)
        dry = self.tuj("promote", node_live["id"], "--confirm", digest, "--bind", "log.py", "--dry-run")
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["binding_status"], "historical")
        self.assertEqual(self.intent_bytes(), canonical_before, "dry run changes nothing")
        _, _, err = self.tu("promote", node_live["id"], "--confirm", digest, "--bind", "log.py", "--binding-status", "active", ok=1)
        self.assertIn("applicability-rationale", err)

        result = self.tuj("promote", node_live["id"], "--confirm", digest, "--bind", "log.py", "--key", "logging-module")
        self.assertEqual(len(result["events"]), 2)
        self.repo.git("add", ".intent")
        self.assertEqual(self.ctl("validate").get("valid"), True)
        self.assertEqual(self.ctl("validate", "--staged").get("valid"), True)
        self.repo.git("commit", "-qm", "record reviewed retrospective intent")

        after = self.intent_bytes()
        for name, data in canonical_before.items():
            if name == "manifests/current.json":
                continue
            self.assertEqual(after[name], data, f"existing canonical record {name} was rewritten")
        self.assertEqual(after["baseline.json"], canonical_before["baseline.json"], "the immutable baseline is untouched")
        intent = self.ctl("show-intent", result["intent_id"])
        self.assertEqual(intent["kind"], "retrospective")
        self.assertIn("NOT captured when the change was made", intent["rationale"])
        binding = self.ctl("show-binding", result["binding_ids"][0])
        self.assertEqual(binding["status"], "historical")
        self.assertEqual(binding["verification"]["status"], "pending")
        events = [e for e in (self.repo.path / ".intent" / "events").glob("*.json") if e.stem in result["events"]]
        self.assertEqual(len(events), 2)
        for path in events:
            event = json.loads(path.read_text())
            self.assertEqual(event["provenance"], "unknown")
            attestations = [v for v in event["verification"] if v["kind"] == "retrospective-trueup"]
            self.assertTrue(attestations and attestations[0]["result"].startswith("NOT verified"))

        # A historical binding records the association but does not govern edits; the ended decision was not promoted.
        governed = self.ctl("explain", "log.py:1")
        self.assertEqual(governed["applicable"], [])
        self.assertEqual(self.ctl("explain", "auth.py:1")["applicable"], [])
        self.assertEqual(self.tuj("show", node_live["id"])["promoted"]["complete"], True)
        _, _, err = self.tu("promote", node_live["id"], "--confirm", digest, "--key", "other-key", ok=1)
        self.assertIn("already promoted", err)

    def test_active_promotion_requires_rationale_and_ended_decisions_cannot_be_active(self):
        self.small_history()
        self.init(providers={"fake": provider_spec()})
        self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        nodes = {n["title"]: n for n in self.graph().nodes.values()}
        ended, live = nodes["Add auth check"], nodes["Add b module"]
        self.adopt_intentctl()
        for node in (ended, live):
            self.tuj("review", node["id"], "--decision", "approve", "--reviewer", "bob")
        ended_hash = self.tuj("show", ended["id"])["review"]["node_hash"]
        live_hash = self.tuj("show", live["id"])["review"]["node_hash"]
        _, _, err = self.tu("promote", ended["id"], "--confirm", ended_hash, "--bind", "auth.py", "--binding-status", "active",
                            "--applicability-rationale", "still applies", ok=1)
        self.assertIn("ended in history", err)
        done = self.tuj("promote", live["id"], "--confirm", live_hash, "--bind", "b.py", "--binding-status", "active",
                        "--applicability-rationale", "b.py must keep initialising x", "--relationship", "constrained-by")
        self.assertEqual(len(done["events"]), 1)
        self.repo.git("add", ".intent")
        self.assertTrue(self.ctl("validate", "--staged")["valid"])
        self.repo.git("commit", "-qm", "promote")
        governed = self.ctl("explain", "b.py:1")
        self.assertEqual(len(governed["applicable"]), 1, "an explicit active binding does govern")
        self.assertEqual(governed["applicable"][0]["binding"]["verification"]["status"], "pending")
        # ended decisions may be promoted only as historical context
        hist = self.tuj("promote", ended["id"], "--confirm", ended_hash, "--bind", "auth.py", "--key", "ended-auth")
        self.assertEqual(hist["intent"]["status"], "superseded" if hist["intent"]["status"] == "superseded" else "retired")

    def test_promotion_refuses_without_intentctl_staged_source_or_changed_discovery(self):
        self.small_history()
        self.init(providers={"fake": provider_spec()})
        self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        node = [n for n in self.graph().nodes.values() if n["title"] == "Add b module"][0]
        self.tuj("review", node["id"], "--decision", "approve", "--reviewer", "carol")
        digest = self.tuj("show", node["id"])["review"]["node_hash"]
        _, _, err = self.tu("promote", node["id"], "--confirm", digest, ok=1)
        self.assertIn("not adopted intentctl", err)
        self.adopt_intentctl()
        self.repo.write("b.py", "x = 2\n")
        self.repo.git("add", "b.py")
        _, _, err = self.tu("promote", node["id"], "--confirm", digest, "--bind", "b.py", ok=1)
        self.assertIn("staged source changes", err)
        self.repo.git("reset", "-q", "b.py")
        self.repo.git("checkout", "--", "b.py")
        _, _, err = self.tu("promote", node["id"], "--confirm", digest, "--bind", "missing.py", ok=1)
        self.assertIn("not present in the index", err)

    def test_everything_is_a_noop_when_unused(self):
        """intentctl behaves exactly as before when no true-up store exists, and never imports the true-up code."""
        self.small_history()
        self.adopt_intentctl()
        out = self.ctl("explain", "auth.py:1")
        self.assertNotIn("retrospective", out)
        env = {**os.environ, **GIT_ENV}
        probe = subprocess.run([sys.executable, "-c",
                                "import sys; sys.path.insert(0, 'tools'); import intentlib.cli as c; "
                                "print('intentlib.trueup' in sys.modules)"], cwd=self.repo.path, env=env, stdout=subprocess.PIPE)
        self.assertEqual(probe.stdout.decode().strip(), "False")
        self.assertTrue(self.ctl("validate")["valid"])
        self.assertTrue(self.ctl("doctor")["valid"])

    def test_broken_store_never_breaks_canonical_queries(self):
        self.small_history()
        self.adopt_intentctl()
        state = self.repo.path / ".git" / "intent-trueup"
        self.tuj("init", "--repo", str(self.repo.path), state=False)
        (state / "meta.json").write_text("{not json")
        out = self.ctl("explain", "auth.py:1", state=state)
        self.assertEqual(out["applicable"], [])
        self.assertIn("skipped", out["retrospective"]["error"])
        self.assertEqual(self.ctl("explain", "auth.py:1", "--no-retrospective", state=state).get("retrospective"), None)


if __name__ == "__main__":
    unittest.main()
