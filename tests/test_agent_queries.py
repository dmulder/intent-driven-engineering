import json

from support import RepoTest, canonical
from intentlib.cli import draft_path
from intentlib.schema import SCHEMAS, check


class AgentQueryTests(RepoTest):
    legacy_files = {"pkg/a.py": "a = 1\n", "pkg/b.py": "b = 2\n", "plain.py": "x = 1\n"}

    def intent(self, event, key, path="auth.py", start=0, end=None, severity=None, relationship="implements",
               related=(), requirements=("Keep behavior",), rationale="Explicit rationale"):
        if not draft_path(self.root, event).exists():
            self.capture(event)
        args = ["intent-create", "--event", event, "--key", key, "--title", key, "--description", "Required " + key,
                "--rationale", rationale]
        for requirement in requirements:
            args += ["--requirement", requirement]
        for other in related:
            args += ["--related", other]
        if severity:
            args += ["--severity", severity]
        i = self.ctl(*args)["id"]
        bind = ["bind", "--event", event, "--key", key, "--intent", i, "--path", path, "--start", str(start),
                "--relationship", relationship, "--rationale", "Bound " + key]
        if end is not None:
            bind += ["--end", str(end)]
        b = self.ctl(*bind)["id"]
        self.ctl("declare", "--event", event, "--intent", i, "--binding", b, "--action", "introduce")
        return i, b

    def test_context_orders_by_severity_and_reports_unknown_regions(self):
        self.intent("first", "advisory", severity="prefer", start=0, end=33)
        self.intent("first", "binding-rule", start=34, end=66)
        self.intent("first", "should-rule", severity="should", start=34, end=66)
        self.finish("first")
        result = self.ctl("context", "auth.py")
        applicable = result["files"][0]["applicable"]
        self.assertEqual([a["intent"]["severity"] for a in applicable], ["must", "should", "prefer"])
        self.assertEqual(applicable[0]["range"]["start_line"], 2)
        self.assertTrue(all(a["live"] for a in applicable))
        file = result["files"][0]
        self.assertGreater(file["bytes"], file["covered_bytes"])
        self.assertEqual(file["uncovered"][-1]["end"], file["bytes"])
        self.assertEqual([g["start"] for g in file["uncovered"]], [33, 66])

    def test_context_scopes_directories_and_rejects_unknown_paths(self):
        self.capture("pkg")
        self.intent("pkg", "pkg-rule", path="pkg/a.py")
        self.finish("pkg")
        result = self.ctl("context", "pkg/")
        self.assertEqual([f["path"] for f in result["files"]], ["pkg/a.py", "pkg/b.py"])
        self.assertEqual(result["summary"]["uncovered_files"], ["pkg/b.py"])
        self.assertEqual(self.ctl("context", ".")["summary"]["files"], len(self.ctl("coverage")["files"]))
        for bad in ("missing.py", "../outside", "pkg/../auth.py"):
            self.assertIn("OPERATION", str(self.ctl("context", bad, ok=False)))

    def test_coverage_is_integer_percent_and_lists_uncovered_files(self):
        self.capture("cov")
        self.intent("cov", "half", start=0, end=33)
        self.finish("cov")
        result = self.ctl("coverage")
        rows = {r["path"]: r for r in result["files"]}
        self.assertEqual(rows["auth.py"]["percent"], 33 * 100 // rows["auth.py"]["bytes"])
        self.assertEqual(rows["plain.py"]["active_bindings"], 0)
        self.assertIn("plain.py", result["summary"]["uncovered_files"])
        self.assertIsInstance(result["summary"]["percent"], int)
        self.assertEqual(self.ctl("coverage", "plain.py")["summary"]["files"], 1)

    def test_list_intents_defaults_to_active_and_filters(self):
        i, b = self.intent("one", "token-policy", requirements=("Tokens expire within one hour",))
        self.intent("one", "other-policy")
        self.finish("one")
        self.assertEqual(len(self.ctl("list-intents")["intents"]), 2)
        found = self.ctl("list-intents", "--contains", "EXPIRE")["intents"]
        self.assertEqual([r["id"] for r in found], [i])
        self.assertEqual(found[0]["files"], ["auth.py"])
        limited = self.ctl("list-intents", "--limit", "1")
        self.assertTrue(limited["truncated"])
        self.assertEqual(limited["total"], 2)
        self.capture("retire")
        self.ctl("intent-revise", i, "--event", "retire", "--status", "retired")
        self.ctl("rebind", b, "--event", "retire", "--status", "ended", "--rationale", "Policy retired")
        self.ctl("declare", "--event", "retire", "--intent", i, "--binding", b, "--action", "retire")
        self.finish("retire")
        self.assertNotIn(i, [r["id"] for r in self.ctl("list-intents")["intents"]])
        self.assertIn(i, [r["id"] for r in self.ctl("list-intents", "--status", "all")["intents"]])
        self.assertEqual([r["id"] for r in self.ctl("list-intents", "--status", "retired")["intents"]], [i])

    def test_freshness_flags_orphans_failed_verification_and_strict_exit(self):
        self.assertEqual(self.ctl("status")["attention"], {})
        self.capture("meta")
        i = self.ctl("intent-create", "--event", "meta", "--key", "meta", "--title", "Metadata only",
                     "--description", "Governs nothing yet", "--rationale", "Explicit rationale")["id"]
        self.ctl("declare", "--event", "meta", "--intent", i, "--action", "introduce")
        self.finish("meta")
        status = self.ctl("status")
        self.assertEqual(status["orphaned_intents"], [i])
        self.assertEqual(status["attention"], {"orphaned_intents": 1})
        self.assertEqual(self.ctl("status", "--strict", ok=False)["attention"], {"orphaned_intents": 1})
        self.assertEqual(self.ctl("doctor")["orphaned_intents"], [i])
        self.capture("bind-it")
        b = self.ctl("bind", "--event", "bind-it", "--key", "bind-it", "--intent", i, "--path", "auth.py",
                     "--rationale", "Now governs code", "--intent-revision", "1")["id"]
        self.ctl("declare", "--event", "bind-it", "--intent", i, "--binding", b, "--action", "preserve")
        self.finish("bind-it")
        self.assertEqual(self.ctl("status", "--strict")["orphaned_intents"], [])
        self.capture("fail")
        self.ctl("rebind", b, "--event", "fail", "--rationale", "Review found a gap", "--verification-status", "failed",
                 "--verification-evidence", "review note")
        self.ctl("declare", "--event", "fail", "--intent", i, "--binding", b, "--action", "rebind")
        self.finish("fail")
        status = self.ctl("status")
        self.assertEqual(status["failed_verification"], [b])
        self.assertEqual(status["verification"], {"failed": 1})
        self.assertEqual(status["attention"], {"failed_verification": 1})

    def test_capture_shows_intent_text_and_severity_where_the_decision_is_made(self):
        i, b = self.intent("rules", "login-limit", severity="should", start=34, end=66,
                           requirements=("Login limit is 10 requests per minute",),
                           rationale="Credential stuffing was observed at 100 per minute")
        self.finish("rules")
        self.write("auth.py", "def authenticate(user, token):\n    return permitted(user, token)\n")
        self.git("add", "auth.py")
        required = self.ctl("capture", "--key", "edit", "--description", "edit", "--rationale", "r")["required_declarations"]
        self.assertEqual(len(required), 1)
        hint = required[0]["intent"]
        self.assertEqual(hint["severity"], "should")
        self.assertIn("Login limit is 10 requests per minute", hint["requirements"])
        self.assertIn("Credential stuffing", hint["rationale"])
        self.assertFalse(hint["needs_reassessment"])
        self.assertEqual(required[0]["verification"]["status"], "declared")

    def test_severity_is_optional_validated_and_revisable(self):
        i, b = self.intent("sev", "legacy-style")
        self.finish("sev")
        self.assertNotIn("severity", self.ctl("show-intent", i))
        self.assertEqual(self.ctl("context", "auth.py")["files"][0]["applicable"][0]["intent"]["severity"], "must")
        self.capture("sev2")
        self.ctl("intent-revise", i, "--event", "sev2", "--severity", "prefer")
        self.ctl("declare", "--event", "sev2", "--intent", i, "--action", "modify")
        self.finish("sev2")
        self.assertEqual(self.ctl("show-intent", i)["severity"], "prefer")
        self.assertNotIn("severity", self.ctl("show-intent", i, "--revision", "1"))
        record = dict(self.ctl("show-intent", i))
        record["severity"] = "never"
        self.assertTrue(check(record, SCHEMAS["intent"]))
        record.pop("severity")
        self.assertFalse(check(record, SCHEMAS["intent"]))
        self.assertIn("invalid choice", self.run_cmd(
            ["python3", "tools/intentctl.py", "intent-create", "--event", "x", "--key", "x", "--title", "t",
             "--description", "d", "--rationale", "r", "--severity", "never"], ok=False).stderr.decode())

    def test_benign_edit_declares_only_the_touched_binding(self):
        # Telos' false-positive control: context must not turn every edit into a ceremony or a rejection.
        data = (self.root / "auth.py").read_bytes()
        split = data.index(b"    if token")
        i_head, b_head = self.intent("regions", "signature", start=0, end=split)
        i_body, b_body = self.intent("regions", "body", start=split)
        self.finish("regions")
        self.write("auth.py", data.replace(b"return False", b"return None"))
        self.git("add", "auth.py")
        required = self.read(self.capture("benign"))["required_declarations"]
        self.assertEqual([r["binding_id"] for r in required], [b_body])
        self.preserve("benign", i_body, b_body, start=split)
        self.finish("benign")
        context = self.ctl("context", "auth.py")["files"][0]
        self.assertEqual({a["binding"]["id"]: a["live"] for a in context["applicable"]}, {b_head: True, b_body: True})
        self.assertEqual(context["uncovered"], [])
        self.assertFalse(any(a["needs_reassessment"] for a in context["applicable"]))

    def test_policy_value_and_cross_module_rules_are_retrievable_by_file(self):
        self.write("limits.py", "LOGIN_RPM = 10\n")
        self.write("boards.py", "def archive(board):\n    board.active = False\n")
        self.write("tasks.py", "def create(board_id):\n    return {'board': board_id}\n")
        self.git("add", "limits.py", "boards.py", "tasks.py")
        self.capture("domain")
        tasks, _ = self.intent("domain", "tasks-reference-live-boards", path="tasks.py",
                               requirements=("Every task references a live board",))
        policy, _ = self.intent("domain", "login-rate-limit", path="limits.py", severity="must",
                                requirements=("Login is limited to 10 requests per minute",),
                                rationale="Decision: 10 rpm; 100 rpm permits credential stuffing")
        guard, _ = self.intent("domain", "archive-must-reassign-tasks", path="boards.py", relationship="constrained-by",
                               related=(tasks,), requirements=("Archiving a board must not orphan its tasks",))
        self.finish("domain")
        limits = self.ctl("context", "limits.py")["files"][0]["applicable"][0]["intent"]
        self.assertIn("credential stuffing", limits["rationale"])
        self.assertIn("10 requests per minute", limits["requirements"][0])
        boards = self.ctl("context", "boards.py")
        self.assertEqual(boards["files"][0]["applicable"][0]["binding"]["relationship"], "constrained-by")
        self.assertEqual([r["id"] for r in boards["related_intents"]], [tasks])
        self.assertEqual(boards["related_intents"][0]["via"], guard)
        self.assertEqual(self.ctl("context", "tasks.py")["related_intents"][0]["id"], guard)
        self.assertNotEqual(policy, guard)

    def test_doctor_reports_missing_intent_heads_with_repair_guidance(self):
        intent, binding = self.bound()
        (self.root / ".intent/intents" / intent / "000001.json").unlink()
        result = self.ctl("doctor", ok=False)
        self.assertFalse(result["valid"])
        self.assertTrue(any(e["code"] == "REFERENCE" and e["path"] == intent for e in result["errors"]))
        self.assertFalse(any(e["code"] == "OPERATION" for e in result["errors"]))
        self.assertIn("repair", result)
        self.assertIn("append corrective revisions", result["repair"])

    def test_historical_queries_accept_pre_severity_schema_but_validate_records(self):
        intent, _ = self.bound()
        schema_path = ".intent/schema/intent.schema.json"
        schema = self.read(schema_path)
        schema["properties"].pop("severity")
        self.write(schema_path, canonical(schema) + b"\n")
        self.git("add", schema_path)
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "pre-severity schema snapshot")
        historical = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.assertIn("SCHEMA_DEFINITION", str(self.ctl("status", ok=False)))
        for argv in (["status"], ["context", "auth.py"], ["coverage"], ["list-intents"],
                     ["explain", "auth.py:2"], ["history", "auth.py:2"], ["show-intent", intent]):
            self.ctl(*argv, "--at", historical)
        record_path = f".intent/intents/{intent}/000001.json"
        record = self.read(record_path)
        record["rationale"] = "tampered without updating the content hash"
        self.write(record_path, canonical(record) + b"\n")
        self.git("add", record_path)
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "damaged historical record")
        self.assertIn("HASH", str(self.ctl("status", "--at", "HEAD", ok=False)))

    def test_json_output_is_canonical_without_floats(self):
        self.intent("canon", "canon")
        self.finish("canon")
        for argv in (["context", "auth.py"], ["coverage"], ["list-intents"], ["status"]):
            raw = self.run_cmd(["python3", "tools/intentctl.py", *argv, "--json"]).stdout
            self.assertEqual(canonical(json.loads(raw)) + b"\n", raw)
