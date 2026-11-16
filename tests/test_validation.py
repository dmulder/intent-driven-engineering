from support import RepoTest, write_json, seal


class ValidationTests(RepoTest):
    def test_missing_event_hook_rejects(self):
        self.write("auth.py", "changed\n")
        self.git("add", "auth.py")
        result = self.ctl("validate", "--staged", ok=False)
        self.assertIn("UNCOVERED_CHANGE", str(result))
        self.assertNotEqual(self.git("commit", "-qm", "bad", ok=False).returncode, 0)

    def test_missing_transition_and_unstaged_metadata(self):
        i, b = self.bound()
        self.write("auth.py", "changed\n")
        self.git("add", "auth.py")
        self.capture("edit")
        failure = self.ctl("finalize", "edit", ok=False)
        self.assertIn("MISSING_TRANSITION", {e["code"] for e in failure["errors"]})
        self.preserve("edit", i, b)
        self.ctl("finalize", "edit")
        self.assertFalse(self.ctl("validate", "--staged", ok=False)["valid"])
        self.git("add", ".intent")
        self.assertTrue(self.ctl("validate", "--staged")["valid"])
        self.commit("explicit accounting")

    def test_preserve_requires_binding_revision(self):
        i, b = self.bound()
        self.write("auth.py", "new implementation\n")
        self.git("add", "auth.py")
        self.capture("edit")
        self.ctl("declare", "--event", "edit", "--intent", i, "--binding", b, "--action", "preserve")
        self.assertIn("STALE_TARGET", str(self.ctl("finalize", "edit", ok=False)))

    def test_historical_rewrite_and_delete_rejected(self):
        i, b = self.bound()
        path = f".intent/intents/{i}/000001.json"
        value = self.read(path)
        value["rationale"] = "rewritten"
        write_json(self.root / path, seal(value))
        self.git("add", path)
        self.assertIn("IMMUTABLE", str(self.ctl("validate", "--staged", ok=False)))
        self.git("restore", "--staged", "--worktree", path)
        self.git("rm", path)
        self.assertFalse(self.ctl("validate", "--staged", ok=False)["valid"])

    def test_invalid_schema_and_reference(self):
        i, b = self.bound()
        path = f".intent/bindings/{b}/000001.json"
        value = self.read(path)
        value["intent_revision"] = 999
        write_json(self.root / path, seal(value))
        self.assertIn("REFERENCE", str(self.ctl("validate", ok=False)))
        value["unexpected"] = 1
        write_json(self.root / path, seal(value))
        self.assertIn("SCHEMA", str(self.ctl("validate", ok=False)))

    def test_partial_staging(self):
        original = (self.root / "auth.py").read_bytes()
        self.write("auth.py", original.replace(b"token.valid", b"token.fresh"))
        self.git("add", "auth.py")
        self.capture("partial")
        self.write("auth.py", "unstaged unrelated version\n")
        self.finish("partial", commit=False, cover=True)
        self.assertTrue(self.ctl("validate", "--staged")["valid"])
        self.assertFalse(self.ctl("validate", ok=False)["valid"])
        self.commit("partial source")
        self.assertEqual((self.root / "auth.py").read_text(), "unstaged unrelated version\n")

    def test_multiple_events_between_commits(self):
        self.write("auth.py", "intermediate\n")
        self.git("add", "auth.py")
        self.capture("first")
        self.finish("first", commit=False, cover=True)
        self.write("auth.py", "final\n")
        self.git("add", "auth.py")
        self.capture("second")
        self.finish("second", cover=True)
        history = self.ctl("history", "auth.py:1")
        self.assertEqual(len(history["events"]), 2)
        self.assertEqual(len(self.read(".intent/manifests/current.json")["events"]), 2)

    def test_new_multifile_event_and_modes(self):
        self.write("new.txt", "é\r\nsecond")
        self.write("empty.txt", b"")
        self.git("add", ".")
        self.capture("new")
        self.finish("new", cover=True)
        self.assertTrue(self.ctl("validate")["valid"])

    def test_deterministic_validation(self):
        self.write("auth.py", "different")
        self.git("add", "auth.py")
        a = self.ctl("validate", "--staged", ok=False)
        b = self.ctl("validate", "--staged", ok=False)
        self.assertEqual(a, b)

    def test_new_source_with_new_intent(self):
        self.write("new.py", "return_value = 42\n")
        self.git("add", "new.py")
        i, b = self.introduce("new-intent", path="new.py")
        self.finish("new-intent")
        self.assertEqual(self.ctl("explain", "new.py:1")["applicable"][0]["intent"]["id"], i)

    def test_single_character_and_multiline_edits(self):
        self.write("auth.py", (self.root / "auth.py").read_bytes().replace(b"True", b"true"))
        self.git("add", "auth.py")
        draft = self.capture("character")
        intervals = self.read(draft)["event"]["changes"][0]["intervals"]
        self.assertEqual(intervals, [{"before": [86, 87], "after": [86, 87]}])
        self.finish("character", cover=True)
        self.write("auth.py", "def authenticate(user, token):\n    return token.valid\n")
        self.git("add", "auth.py")
        self.capture("multiline")
        self.finish("multiline", cover=True)

    def test_unknown_provenance_rejects_unbound_new_bytes(self):
        self.write("auth.py", "brand new implementation\n")
        self.git("add", "auth.py")
        self.capture("escape")
        result = self.ctl("finalize", "escape", ok=False)
        self.assertIn("NEW_CODE_REQUIRES_INTENT", {e["code"] for e in result["errors"]})

    def test_unrelated_binding_survives_without_rebind(self):
        self.write("auth.py", "HEAD\nTAIL\n")
        self.git("add", "auth.py")
        self.capture("seed")
        head_i, head_b = self.introduce("head", path="auth.py", start=0, end=5, event="seed")
        tail_i, tail_b = self.introduce("tail", path="auth.py", start=5, end=10, event="seed")
        self.finish("seed")
        self.write("auth.py", "HEAD\nTAIL!\n")
        self.git("add", "auth.py")
        draft = self.capture("tail-edit")
        required = {item["binding_id"] for item in self.read(draft)["required_declarations"]}
        self.assertEqual(required, {tail_b})
        self.preserve("tail-edit", tail_i, tail_b, start=5, end=11,
                      verification_status="pending")
        self.finish("tail-edit")
        explain = self.ctl("explain", "auth.py:1")
        self.assertEqual({item["binding"]["id"] for item in explain["applicable"]}, {head_b})
        self.assertFalse(explain["applicable"][0]["target_matches"])
        self.assertEqual(explain["applicable"][0]["live_target"]["end"], 5)
        tail = self.ctl("show-binding", tail_b)
        self.assertEqual(tail["verification"]["status"], "pending")

    def test_verified_binding_requires_evidence(self):
        i, b = self.bound()
        self.capture("verify")
        self.ctl("rebind", b, "--event", "verify", "--rationale", "Claim verified without evidence",
                 "--verification-status", "verified")
        self.ctl("declare", "--event", "verify", "--intent", i, "--binding", b, "--action", "rebind")
        result = self.ctl("finalize", "verify", ok=False)
        self.assertIn("VERIFICATION", {e["code"] for e in result["errors"]})

    def test_schema_definition_pin_is_proposed_only(self):
        from intentlib.core import Store, snapshot
        base = snapshot(self.root, revision="HEAD")
        # Base schemas may predate the running validator during a schema upgrade.
        self.assertEqual(Store(base, enforce_schema_definitions=False).errors, [])

    def test_capture_does_not_declare_preservation(self):
        i, b = self.bound()
        self.write("auth.py", "replacement\n")
        self.git("add", "auth.py")
        result = self.ctl("capture", "--key", "draft-only", "--description", "draft", "--rationale", "draft")
        draft = self.read(result["draft"])
        self.assertEqual(draft["event"]["intent_transitions"], [])
        self.assertIsNone(draft["event"]["provenance"])
        self.assertEqual(draft["required_declarations"][0]["binding_id"], b)

    def test_schema_definition_tampering(self):
        self.write(".intent/schema/event.schema.json", "{}\n")
        self.git("add", ".intent/schema/event.schema.json")
        result = self.ctl("validate", "--staged", ok=False)
        self.assertIn("SCHEMA_DEFINITION", str(result))

    def test_declared_diff_cannot_hide_changed_bytes(self):
        self.write("auth.py", "all new source\n")
        self.git("add", "auth.py")
        path = self.capture("tamper-range")
        draft = self.read(path)
        draft["event"]["changes"][0]["intervals"] = []
        write_json(self.root / path, draft)
        self.assertIn("COVERAGE", str(self.ctl("finalize", "tamper-range", ok=False)))

    def test_known_provenance_cannot_claim_unbound_legacy(self):
        self.write("auth.py", "unknown legacy transformation\n")
        self.git("add", "auth.py")
        self.capture("known", provenance="known")
        self.assertIn("UNKNOWN_PROVENANCE", str(self.ctl("finalize", "known", ok=False)))

    def test_manifest_cannot_hide_binding(self):
        i, b = self.bound()
        path = ".intent/manifests/current.json"
        value = self.read(path)
        del value["bindings"][b]
        write_json(self.root / path, value)
        self.assertIn("HEAD", str(self.ctl("validate", ok=False)))

    def test_missing_binding_reference_is_rejected(self):
        i, b = self.bound()
        self.capture("invalid-binding")
        self.ctl("declare", "--event", "invalid-binding", "--intent", i,
                  "--binding", "B-" + "0" * 64, "--action", "preserve")
        result = self.ctl("finalize", "invalid-binding", ok=False)
        self.assertIn("REFERENCE", {e["code"] for e in result["errors"]})

    def test_unresolved_binding_must_resolve_before_further_source_edit(self):
        i, b = self.bound()
        self.capture("uncertain")
        self.ctl("rebind", b, "--event", "uncertain", "--status", "unresolved", "--rationale", "Needs assessment")
        self.ctl("declare", "--event", "uncertain", "--intent", i, "--binding", b, "--action", "rebind")
        self.finish("uncertain")
        self.write("auth.py", "edited\n")
        self.git("add", "auth.py")
        self.capture("must-resolve")
        self.preserve("must-resolve", i, b, status="unresolved")
        result = self.ctl("finalize", "must-resolve", ok=False)
        self.assertIn("UNRESOLVED", {e["code"] for e in result["errors"]})

    def test_unapplication_requires_binding_revision_without_source_edit(self):
        i, b = self.bound()
        self.capture("unapply-only")
        self.ctl("declare", "--event", "unapply-only", "--intent", i, "--binding", b, "--action", "unapply")
        result = self.ctl("finalize", "unapply-only", ok=False)
        self.assertIn("MISSING_BINDING_REVISION", {e["code"] for e in result["errors"]})
