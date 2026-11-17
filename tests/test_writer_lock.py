import fcntl
import json
import os
import subprocess
import sys

from support import RepoTest
from intentlib.cli import MUTATING, draft_path, parser


class WriterLockTests(RepoTest):
    def lock_path(self):
        return self.root / ".git/intentctl.lock"

    def hold(self):
        descriptor = os.open(self.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor

    def test_writers_fail_fast_while_lock_is_held_and_readers_are_unaffected(self):
        descriptor = self.hold()
        result = self.ctl("capture", "--key", "blocked", "--description", "d", "--rationale", "r", ok=False)
        self.assertIn("another intentctl writer", str(result))
        self.assertFalse(draft_path(self.root, "blocked").exists())
        self.assertEqual(self.ctl("status")["events"], 0)
        self.assertTrue(self.ctl("validate")["valid"])
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        self.assertIn("draft", self.ctl("capture", "--key", "blocked", "--description", "d", "--rationale", "r"))

    def test_every_mutating_command_is_locked_and_every_other_command_is_not(self):
        names = set(parser()._subparsers._group_actions[0].choices)
        self.assertLessEqual(MUTATING, names)
        read_only = names - MUTATING
        self.assertEqual(read_only, {"validate", "status", "doctor", "explain", "history", "show-intent",
                                     "show-binding", "impact", "context", "coverage", "list-intents",
                                     "audit", "reconcile", "report", "watch"})

    def test_concurrent_binds_never_lose_a_draft_update(self):
        self.capture("race")
        intent = self.ctl("intent-create", "--event", "race", "--key", "race", "--title", "t",
                          "--description", "d", "--rationale", "r")["id"]
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"}
        processes = [subprocess.Popen(
            [sys.executable, "tools/intentctl.py", "bind", "--event", "race", "--key", f"b{n}", "--intent", intent,
             "--path", "auth.py", "--start", str(n), "--end", str(n + 1), "--rationale", "r", "--json"],
            cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for n in range(6)]
        outcomes = []
        for process in processes:
            out, _ = process.communicate()
            outcomes.append((process.returncode, out.decode()))
        succeeded = [out for code, out in outcomes if code == 0]
        failed = [out for code, out in outcomes if code != 0]
        draft = self.read(str(draft_path(self.root, "race").relative_to(self.root)))
        self.assertEqual({b["id"] for b in draft["bindings"]}, {json.loads(out)["id"] for out in succeeded},
                         "a successful bind was lost from the draft")
        self.assertTrue(succeeded)
        self.assertTrue(failed, "processes never overlapped; the test is not exercising contention")
        for out in failed:
            self.assertIn("another intentctl writer", out)


class OutputContractTests(RepoTest):
    def test_command_results_carry_output_version_and_records_keep_schema_version(self):
        i, _ = self.bound()
        for argv in (["status"], ["validate"], ["explain", "auth.py:2"], ["history", "auth.py:2"], ["impact", i],
                     ["context", "auth.py"], ["coverage"], ["list-intents"], ["doctor"]):
            self.assertEqual(self.ctl(*argv)["output_version"], 1, argv)
        record = self.ctl("show-intent", i)
        self.assertEqual(record["schema_version"], 1)
        self.assertNotIn("output_version", record)

    def test_failures_are_versioned_and_actionable(self):
        result = self.ctl("explain", "missing.py:1", ok=False)
        self.assertEqual(result["output_version"], 1)
        self.assertEqual(result["errors"][0]["code"], "OPERATION")
