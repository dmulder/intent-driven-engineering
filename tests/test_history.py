from support import RepoTest


class HistoryTests(RepoTest):
    def test_intent_only_revision_and_implementation_update(self):
        i, b = self.bound()
        old_commit = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.capture("new-policy")
        self.ctl("intent-revise", i, "--event", "new-policy", "--requirement", "Reject expired tokens")
        self.ctl("declare", "--event", "new-policy", "--intent", i, "--action", "modify")
        self.finish("new-policy")
        self.assertEqual(self.ctl("status")["reassessment"], [b])
        self.assertTrue(self.ctl("impact", i)["bindings"][0]["needs_reassessment"])
        self.assertEqual(self.ctl("explain", "auth.py:2", "--at", old_commit)["applicable"][0]["intent"]["revision"], 1)
        self.write("auth.py", "def authenticate(user, token):\n    return token.valid and not token.expired and not user.locked\n")
        self.git("add", "auth.py")
        self.capture("implement")
        self.preserve("implement", i, b, intent_revision=2)
        self.finish("implement")
        self.assertEqual(self.ctl("status")["reassessment"], [])
        history = self.ctl("history", "auth.py:2")
        self.assertEqual([v["revision"] for v in history["intent_revisions"]], [1, 2])
        self.assertEqual(len(history["events"]), 3)

    def test_retirement_keeps_causation(self):
        i, b = self.bound()
        self.capture("retire")
        self.ctl("intent-revise", i, "--event", "retire", "--status", "retired", "--rationale", "Policy no longer required")
        self.ctl("rebind", b, "--event", "retire", "--status", "historical", "--rationale", "Preserve original causation")
        self.ctl("declare", "--event", "retire", "--intent", i, "--binding", b, "--action", "retire")
        self.finish("retire")
        self.assertEqual(self.ctl("explain", "auth.py:2")["applicable"], [])
        self.assertEqual(len(self.ctl("history", "auth.py:2")["intent_revisions"]), 2)

    def test_supersession_and_cycles(self):
        i, b = self.bound()
        self.capture("supersede")
        new_i = self.ctl("intent-create", "--event", "supersede", "--key", "replacement", "--title", "Replacement",
                         "--description", "New policy", "--rationale", "New understanding", "--related", i)["id"]
        self.ctl("intent-revise", i, "--event", "supersede", "--status", "superseded", "--related", new_i)
        self.ctl("rebind", b, "--event", "supersede", "--status", "historical", "--rationale", "Old policy superseded")
        self.ctl("declare", "--event", "supersede", "--intent", i, "--binding", b, "--action", "supersede")
        self.ctl("declare", "--event", "supersede", "--intent", new_i, "--action", "introduce")
        self.finish("supersede")
        self.assertEqual(self.ctl("impact", i)["related_intents"], [new_i])

    def test_compensating_revert(self):
        i, b = self.bound()
        original = (self.root / "auth.py").read_bytes()
        self.write("auth.py", "replacement\n")
        self.git("add", "auth.py")
        self.capture("change")
        self.preserve("change", i, b)
        self.finish("change")
        self.git("revert", "--no-commit", "HEAD")
        self.assertFalse(self.ctl("validate", "--staged", ok=False)["valid"])
        # Git sequencer operations may bypass pre-commit; CI/pre-push remain required.
        # Restore immutable metadata, keeping only the source inverse.
        self.git("restore", "--source=HEAD", "--staged", "--worktree", ".intent")
        self.capture("compensating-revert")
        self.preserve("compensating-revert", i, b)
        self.finish("compensating-revert")
        self.assertEqual((self.root / "auth.py").read_bytes(), original)
        self.assertEqual(len(self.ctl("history", "auth.py:1")["events"]), 3)

    def test_intent_and_code_revision_together(self):
        i, b = self.bound()
        self.write("auth.py", "def authenticate(user, token):\n    return token.valid and token.fresh and not user.locked\n")
        self.git("add", "auth.py")
        self.capture("both")
        self.ctl("intent-revise", i, "--event", "both", "--requirement", "Require freshness")
        self.ctl("rebind", b, "--event", "both", "--path", "auth.py", "--intent-revision", "2", "--rationale", "Implements revised policy")
        self.ctl("declare", "--event", "both", "--intent", i, "--binding", b, "--action", "modify")
        self.finish("both")
        self.assertEqual(self.ctl("explain", "auth.py:2")["applicable"][0]["intent"]["revision"], 2)
