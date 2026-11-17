import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

from support import PROJECT

spec = importlib.util.spec_from_file_location("ci_validate", PROJECT / "scripts/ci_validate.py")
ci_validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_validate)


class ValidationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Tests")
        self.git("config", "user.email", "tests@example.invalid")
        self.base = self.commit("accepted")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.head = self.commit("proposed")

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "commit.gpgsign=false", *args], cwd=self.root).decode().strip()

    def commit(self, message):
        (self.root / "file").write_text(message)
        self.git("add", "file")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def command(self, base, event="push"):
        return ci_validate.validation_command(self.root, base, event, "main")

    def test_regular_push_validates_the_event_range(self):
        self.assertEqual(self.command(self.base)[-2:], ["--range", self.base + "..HEAD"])

    def test_missing_before_after_force_push_uses_accepted_default_branch(self):
        self.assertEqual(self.command("f" * 40)[-2:], ["--range", self.base + "..HEAD"])

    def test_existing_divergent_before_does_not_revalidate_accepted_commits(self):
        self.git("checkout", "--detach", self.base)
        old_head = self.commit("old branch")
        self.git("checkout", "--detach", self.head)
        self.assertEqual(self.command(old_head)[-2:], ["--range", self.base + "..HEAD"])

    def test_pull_request_keeps_its_explicit_base_even_when_missing(self):
        self.assertEqual(self.command("f" * 40, "pull_request")[-1], "f" * 40 + "..HEAD")

    def test_branch_creation_still_validates_head_only(self):
        self.assertEqual(self.command("0" * 40)[-2:], ["--commit", "HEAD"])

    def test_default_branch_rewrite_fails_instead_of_validating_an_empty_range(self):
        self.git("update-ref", "refs/remotes/origin/main", self.head)
        with self.assertRaisesRegex(ValueError, "explicit base"):
            self.command("f" * 40)

    def test_missing_trusted_history_fails_closed(self):
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        with self.assertRaisesRegex(ValueError, "fetch full branch history"):
            self.command("f" * 40)
