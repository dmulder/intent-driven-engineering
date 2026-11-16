from support import RepoTest


class BindingTests(RepoTest):
    def test_three_overlapping_intents(self):
        pairs = [self.bound(key=k, start=34, end=66) for k in ("valid", "locked", "offline")]
        self.assertEqual(len(self.ctl("explain", "auth.py:2")["applicable"]), 3)
        self.write("auth.py", "def authenticate(user, token):\n    return permitted(user, token)\n")
        self.git("add", "auth.py")
        self.capture("refactor")
        for i, b in pairs[:2]:
            self.preserve("refactor", i, b)
        self.assertIn("MISSING_TRANSITION", str(self.ctl("finalize", "refactor", ok=False)))
        self.preserve("refactor", *pairs[2])
        self.finish("refactor")
        self.assertEqual(len(self.ctl("explain", "auth.py:2")["applicable"]), 3)

    def test_one_intent_multiple_files_and_partial_overlap(self):
        i, b = self.bound(start=0, end=80)
        self.write("other.py", "other implementation\n")
        self.git("add", "other.py")
        self.capture("other")
        second = self.ctl("bind", "--event", "other", "--key", "second", "--intent", i,
                           "--path", "other.py", "--rationale", "Same requirement elsewhere")["id"]
        self.ctl("declare", "--event", "other", "--intent", i, "--binding", second, "--action", "preserve")
        self.finish("other")
        self.assertEqual(len(self.ctl("impact", i)["bindings"]), 2)
        self.assertTrue(self.ctl("explain", "auth.py:1")["applicable"])
        self.assertTrue(self.ctl("explain", "auth.py:3")["applicable"])

    def test_explicit_move_copy_split_consolidation(self):
        i, b = self.bound()
        data = (self.root / "auth.py").read_bytes()
        self.git("mv", "auth.py", "moved.py")
        self.capture("move")
        self.preserve("move", i, b, path="moved.py", action="rebind")
        self.finish("move")
        self.assertTrue(self.ctl("explain", "moved.py:2")["applicable"])
        self.write("copy.py", data)
        self.git("add", "copy.py")
        self.capture("copy")
        second = self.ctl("bind", "--event", "copy", "--key", "copy", "--intent", i, "--path", "copy.py",
                           "--predecessor", b, "--rationale", "Explicit copy lineage")["id"]
        self.ctl("declare", "--event", "copy", "--intent", i, "--binding", second, "--action", "rebind")
        self.finish("copy")
        self.assertEqual(len(self.ctl("impact", i)["bindings"]), 2)
        self.assertEqual(len(self.ctl("history", "copy.py:2")["binding_revisions"]), 3)
        # Split/consolidation are explicit many-to-many predecessor edges, not inference.
        self.capture("consolidate")
        self.ctl("rebind", second, "--event", "consolidate", "--status", "ended", "--rationale", "Consolidate in moved implementation")
        self.ctl("declare", "--event", "consolidate", "--intent", i, "--binding", second, "--action", "unapply")
        self.finish("consolidate")
        self.assertEqual(len(self.ctl("impact", i)["bindings"]), 1)

    def test_deletion_requires_explicit_unapplication(self):
        i, b = self.bound()
        self.git("rm", "auth.py")
        self.capture("delete")
        self.ctl("rebind", b, "--event", "delete", "--status", "historical", "--rationale", "Implementation removed")
        self.ctl("declare", "--event", "delete", "--intent", i, "--binding", b, "--action", "unapply")
        self.finish("delete")
        self.assertEqual(self.ctl("impact", i)["bindings"], [])
        self.assertEqual(self.ctl("show-intent", i)["status"], "active")
        self.assertTrue(self.ctl("history", "auth.py:1")["binding_revisions"])

    def test_unicode_crlf_and_insertion_boundary(self):
        self.write("auth.py", "é\r\nabc")
        self.git("add", "auth.py")
        self.capture("encoding")
        self.finish("encoding")
        i, b = self.bound(start=0, end=2)
        self.assertEqual(self.ctl("explain", "auth.py:1")["applicable"][0]["binding"]["target"]["end"], 2)
        self.write("auth.py", "éX\r\nabc")
        self.git("add", "auth.py")
        result = self.capture("boundary")
        self.assertEqual(len(self.read(result)["required_declarations"]), 1)
        self.preserve("boundary", i, b, start=0, end=2)
        self.finish("boundary")

    def test_split_and_consolidate_explicit_lineage(self):
        i, original = self.bound()
        self.write("auth.py", "def valid(token):\n    return token.valid\n\ndef unlocked(user):\n    return not user.locked\n")
        self.git("add", "auth.py")
        self.capture("split")
        self.ctl("rebind", original, "--event", "split", "--status", "historical", "--rationale", "Replaced by two fragments")
        self.ctl("declare", "--event", "split", "--intent", i, "--binding", original, "--action", "unapply")
        left = self.ctl("bind", "--event", "split", "--key", "valid-helper", "--intent", i, "--path", "auth.py",
                        "--start", "0", "--end", "40", "--predecessor", original, "--rationale", "First split successor")["id"]
        right = self.ctl("bind", "--event", "split", "--key", "unlocked-helper", "--intent", i, "--path", "auth.py",
                         "--start", "40", "--predecessor", original, "--rationale", "Second split successor")["id"]
        self.ctl("declare", "--event", "split", "--intent", i, "--binding", left, "--action", "rebind")
        self.ctl("declare", "--event", "split", "--intent", i, "--binding", right, "--action", "rebind")
        self.finish("split")
        self.assertEqual(len(self.ctl("impact", i)["bindings"]), 2)
        self.write("auth.py", "def eligible(user, token):\n    return token.valid and not user.locked\n")
        self.git("add", "auth.py")
        self.capture("join")
        self.ctl("rebind", left, "--event", "join", "--status", "historical", "--rationale", "Consolidated")
        self.ctl("rebind", right, "--event", "join", "--status", "historical", "--rationale", "Consolidated")
        self.ctl("declare", "--event", "join", "--intent", i, "--binding", left, "--action", "unapply")
        self.ctl("declare", "--event", "join", "--intent", i, "--binding", right, "--action", "unapply")
        joined = self.ctl("bind", "--event", "join", "--key", "joined", "--intent", i, "--path", "auth.py",
                          "--predecessor", left, "--predecessor", right, "--rationale", "Explicit consolidation")["id"]
        self.ctl("declare", "--event", "join", "--intent", i, "--binding", joined, "--action", "rebind")
        self.finish("join")
        self.assertEqual(self.ctl("show-binding", joined)["predecessors"], [left, right])
        self.assertEqual(len(self.ctl("history", "auth.py:2")["binding_revisions"]), 7)
