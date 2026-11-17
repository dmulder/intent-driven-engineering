import fcntl
import json
import os
import select
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
        # Hold the real CLI lock across an explicit pipe handshake. Every contender
        # must finish before the holder is released, regardless of process scheduling.
        holder_code = """
import os, sys
from contextlib import contextmanager
sys.path.insert(0, 'tools')
from intentlib import cli
lock = cli.writer_lock
@contextmanager
def gated_lock(root):
    with lock(root):
        os.write(int(sys.argv[1]), b'1')
        if sys.stdin.buffer.read(1) != b'1':
            raise RuntimeError('holder was not released')
        yield
cli.writer_lock = gated_lock
raise SystemExit(cli.main(sys.argv[2:]))
"""
        def bind_args(key, start):
            return ["bind", "--event", "race", "--key", key, "--intent", intent,
                    "--path", "auth.py", "--start", str(start), "--end", str(start + 1),
                    "--rationale", "r", "--json"]

        def cleanup(process):
            if process.poll() is None:
                process.kill()
            process.communicate()

        ready, signal = os.pipe()
        self.addCleanup(os.close, ready)
        try:
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code, str(signal), *bind_args("holder", 0)],
                cwd=self.root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, pass_fds=(signal,))
        finally:
            os.close(signal)
        self.addCleanup(cleanup, holder)
        self.assertTrue(select.select([ready], [], [], 30)[0], "holder never acquired its lock")
        self.assertEqual(os.read(ready, 1), b"1")
        contenders = []
        for n in range(6):
            process = subprocess.Popen([sys.executable, "tools/intentctl.py", *bind_args(f"b{n}", n + 1)],
                                       cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(cleanup, process)
            contenders.append(process)
        for process in contenders:
            out, err = process.communicate(timeout=30)
            self.assertNotEqual(process.returncode, 0, (out, err))
            self.assertIn("another intentctl writer", out.decode())
        self.assertEqual(self.read(str(draft_path(self.root, "race").relative_to(self.root)))["bindings"], [])
        out, err = holder.communicate(input=b"1", timeout=30)
        self.assertEqual(holder.returncode, 0, (out, err))
        accepted = {json.loads(out)["id"]}
        # Each rejected writer can retry after release without losing earlier writes.
        for n in range(6):
            accepted.add(self.ctl(*bind_args(f"b{n}", n + 1)[:-1])["id"])
        draft = self.read(str(draft_path(self.root, "race").relative_to(self.root)))
        self.assertEqual(len(accepted), 7)
        self.assertEqual({b["id"] for b in draft["bindings"]}, accepted)



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
