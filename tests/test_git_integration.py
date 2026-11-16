import os
from support import RepoTest


class GitTests(RepoTest):
    def test_idempotent_init_and_hook_preservation(self):
        self.ctl("uninstall-hooks")
        hook = self.root / ".git/hooks/pre-commit"
        self.write(".git/hooks/pre-commit", "#!/bin/sh\necho ran > hook-ran\n")
        hook.chmod(0o755)
        self.ctl("init")
        self.ctl("init")
        self.git("commit", "--allow-empty", "-qm", "empty")
        self.assertTrue((self.root / "hook-ran").exists())
        self.ctl("uninstall-hooks")
        self.assertIn("echo ran", hook.read_text())

    def test_binary_symlink_and_mode_change(self):
        self.write("blob.bin", b"a\0b")
        os.symlink("auth.py", self.root / "link")
        self.git("add", ".")
        self.capture("opaque")
        self.finish("opaque", cover=True)
        i, b = self.bound(key="binary", path="blob.bin")
        self.write("blob.bin", b"c\0d")
        self.git("add", "blob.bin")
        self.capture("binary-change")
        self.finish("binary-change", cover=True)
        (self.root / "auth.py").chmod(0o755)
        self.git("add", "auth.py")
        self.capture("mode")
        self.finish("mode")

    def test_detached_head_and_worktree(self):
        self.git("checkout", "--detach", "-q")
        self.ctl("validate", "--staged")
        linked = self.root / "linked"
        self.git("worktree", "add", "-qb", "linked", str(linked))
        import subprocess, sys
        result = subprocess.run([sys.executable, "tools/intentctl.py", "validate", "--staged", "--json"], cwd=linked, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_merge_is_explicitly_rejected(self):
        self.git("checkout", "-qb", "side")
        self.git("commit", "--allow-empty", "-qm", "side")
        self.git("checkout", "-q", "-")
        self.git("commit", "--allow-empty", "-qm", "main")
        self.git("merge", "--no-commit", "--no-ff", "side")
        self.assertIn("MERGE_UNSUPPORTED", str(self.ctl("validate", "--staged", ok=False)))

    def test_cherry_pick_rebase_squash_and_range_validation(self):
        base = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("checkout", "-qb", "topic")
        self.write("new.txt", "topic\n")
        self.git("add", "new.txt")
        self.capture("topic")
        self.finish("topic", cover=True)
        topic = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("checkout", "-qb", "receiver", base)
        self.git("cherry-pick", topic)
        self.ctl("validate", "--range", base + "..HEAD")
        self.git("checkout", "-qb", "squashed", base)
        self.git("merge", "--squash", "topic")
        self.ctl("validate", "--staged")
        self.commit("squash")
        self.git("checkout", "-q", "topic")
        self.git("rebase", "receiver")
        self.ctl("validate", "--commit", "HEAD")

    def test_metadata_conflict_and_concurrent_revision_collision(self):
        i, b = self.bound()
        path = f".intent/intents/{i}/000001.json"
        self.write(path, "<<<<<<< branch\n{}\n=======\n{}\n>>>>>>> other\n")
        self.assertIn("PARSE", str(self.ctl("validate", ok=False)))
        self.git("restore", path)
        self.capture("collision")
        result = self.ctl("intent-create", "--event", "collision", "--key", "tokens", "--title", "collision",
                          "--description", "collision", "--rationale", "collision", ok=False)
        self.assertIn("already exists", str(result))

    def test_pre_push_rejects_commit_that_bypassed_pre_commit(self):
        base = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.write("auth.py", "unaccounted change\n")
        self.git("add", "auth.py")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "bypassed hook")
        tip = self.git("rev-parse", "HEAD").stdout.decode().strip()
        data = f"refs/heads/main {tip} refs/heads/main {base}\n".encode()
        result = self.run_cmd([".git/hooks/pre-push", "origin", "unused"], ok=False, input=data)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"UNCOVERED_CHANGE", result.stdout)

    def test_existing_pre_push_receives_original_stdin(self):
        self.ctl("uninstall-hooks")
        self.write(".git/hooks/pre-push", "#!/bin/sh\ncat > received-stdin\n")
        (self.root / ".git/hooks/pre-push").chmod(0o755)
        self.ctl("init")
        tip = self.git("rev-parse", "HEAD").stdout.decode().strip()
        data = f"refs/heads/main {tip} refs/heads/main {tip}\n".encode()
        self.run_cmd([".git/hooks/pre-push", "origin", "unused"], input=data)
        self.assertEqual((self.root / "received-stdin").read_bytes(), data)

    def test_core_hooks_path_is_respected_and_restored(self):
        self.ctl("uninstall-hooks")
        self.git("config", "core.hooksPath", "custom-hooks")
        self.write("custom-hooks/pre-commit", "#!/bin/sh\nexit 0\n")
        (self.root / "custom-hooks/pre-commit").chmod(0o755)
        self.ctl("init")
        self.assertTrue((self.root / "custom-hooks/pre-commit.intentctl-original").exists())
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), b"custom-hooks")
        self.ctl("uninstall-hooks")
        self.assertEqual((self.root / "custom-hooks/pre-commit").read_text(), "#!/bin/sh\nexit 0\n")

    def test_divergent_same_intent_revision_conflicts(self):
        i, b = self.bound()
        base = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("checkout", "-qb", "left")
        self.capture("left-revision")
        self.ctl("intent-revise", i, "--event", "left-revision", "--requirement", "left policy")
        self.ctl("declare", "--event", "left-revision", "--intent", i, "--action", "modify")
        self.finish("left-revision")
        self.git("checkout", "-qb", "right", base)
        self.capture("right-revision")
        self.ctl("intent-revise", i, "--event", "right-revision", "--requirement", "right policy")
        self.ctl("declare", "--event", "right-revision", "--intent", i, "--action", "modify")
        self.finish("right-revision")
        self.assertNotEqual(self.git("merge", "--squash", "left", ok=False).returncode, 0)
        self.assertIn("unmerged index", str(self.ctl("validate", "--staged", ok=False)))

    def test_pre_adoption_merge_history_is_unknown_not_rejected(self):
        import shutil
        other = self.root / "legacy"
        other.mkdir()
        self.run_cmd(["git", "-C", str(other), "init", "-q"])
        self.run_cmd(["git", "-C", str(other), "config", "user.name", "Legacy"])
        self.run_cmd(["git", "-C", str(other), "config", "user.email", "legacy@example.invalid"])
        self.run_cmd(["git", "-C", str(other), "commit", "--allow-empty", "-qm", "root"])
        self.run_cmd(["git", "-C", str(other), "checkout", "-qb", "side"])
        self.run_cmd(["git", "-C", str(other), "commit", "--allow-empty", "-qm", "side"])
        self.run_cmd(["git", "-C", str(other), "checkout", "-q", "-"])
        self.run_cmd(["git", "-C", str(other), "merge", "--no-ff", "-qm", "legacy merge", "side"])
        shutil.copytree(self.root / "tools", other / "tools", ignore=shutil.ignore_patterns("__pycache__"))
        import sys
        import subprocess, json
        result = subprocess.run([sys.executable, "tools/intentctl.py", "validate", "--range", "HEAD", "--json"], cwd=other, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(json.loads(result.stdout)["valid"])
