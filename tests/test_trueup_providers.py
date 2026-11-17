"""AI provider layer: classification, ordered fallback, retries, repair, circuit breaking, budgets, chunking.
Only scripted fake CLIs are executed; no model, no network."""
import json
import os
from pathlib import Path
import signal
import sys
import textwrap
import threading
import time
import unittest

from trueup_support import FAKE_AI, TrueUpTest, provider_spec, script_provider  # noqa: F401
from intentlib.trueup import analysis, providers as prov
from intentlib.trueup.providers import (Budget, BudgetExhausted, CallFailed, Caller, Circuits, OversizeError, Parsed, Provider, build_providers,
                                        classify_failure, default_repair)

VALID = json.dumps({"no_intent_found": True, "findings": []})


class ScriptCase(TrueUpTest):
    def script(self, name, body):
        path = self.tmp / f"{name}.py"
        path.write_text("import os, sys, json, time\nPROMPT = sys.stdin.read()\n" + textwrap.dedent(body))
        return path

    def provider(self, name, body, **extra):
        spec = script_provider(self.script(name, body), **extra)
        return Provider(name, spec)

    def counter(self, name):
        return str(self.tmp / f"{name}.count")

    def caller(self, providers, **kw):
        sleeps = []
        kw.setdefault("sleep", sleeps.append)
        kw.setdefault("backoff_base", 2.0)
        caller = Caller(providers, **kw)
        caller.sleeps = sleeps
        return caller

    @staticmethod
    def ok_parser(text):
        try:
            json.loads(text)
        except ValueError:
            return Parsed(None, ["not json"], "malformed")
        return Parsed(text)


class Classification(unittest.TestCase):
    def test_failure_classes(self):
        cases = {
            "Error: not logged in. Please run login": prov.AUTH,
            "401 Unauthorized": prov.AUTH,
            "You exceeded your current quota, check billing": prov.QUOTA,
            "429 Too Many Requests: rate limit": prov.RATE_LIMIT,
            "model is overloaded, try again later": prov.RATE_LIMIT,
            "ECONNRESET while contacting api": prov.TRANSIENT,
            "503 service unavailable": prov.TRANSIENT,
            "prompt is too long for the context window": prov.CONTEXT,
            "error: unknown option '--sandbox'": prov.CONFIG,
            "segfault": prov.NONZERO,
        }
        for text, expected in cases.items():
            self.assertEqual(classify_failure(1, "", text), expected, text)

    def test_successful_answer_discussing_rate_limits_is_not_a_failure(self):
        long_answer = "x" * 5000 + " rate limit quota unauthorized"
        self.assertEqual(classify_failure(0, long_answer, ""), prov.NONZERO)  # nothing to classify on success
        self.assertEqual(classify_failure(1, long_answer, ""), prov.NONZERO, "long stdout of a failed run is not scanned")

    def test_builtin_templates_exist_for_all_required_clis(self):
        for name in ("codex", "claude", "gemini", "opencode"):
            self.assertIn(name, prov.BUILTIN_PROVIDERS)
            provider = build_providers([name])[0]
            self.assertTrue(provider.invocations)
        codex = build_providers(["codex"])[0].invocations[0]["argv"]
        self.assertIn("read-only", codex, "read-only permissions requested where the CLI supports them")
        with self.assertRaises(Exception):
            build_providers(["nosuchcli"])

    def test_commands_are_configurable_and_models_inserted(self):
        provider = Provider("custom", {"bin": "/opt/x/tool", "invocations": [{"argv": ["{bin}", "ask", "--stdin"], "stdin": True}],
                                       "model_args": ["--model", "{model}"], "model_after": 1}, {"model": "m1"})
        self.assertEqual(provider._argv(provider.invocations[0]["argv"], "p", "/w", "/w/o", "/w/p"),
                         ["/opt/x/tool", "--model", "m1", "ask", "--stdin"])


class Fallback(ScriptCase):
    def test_ordered_fallback_and_permanent_failures_disable_a_provider(self):
        a = self.provider("a", f"open({self.counter('a')!r}, 'a').write('x'); sys.stderr.write('not logged in'); sys.exit(1)")
        b = self.provider("b", f"open({self.counter('b')!r}, 'a').write('x'); print({VALID!r})")
        caller = self.caller([a, b])
        first = caller.complete("task", self.ok_parser)
        self.assertEqual(first["provider"], "b")
        self.assertEqual(first["log"][0]["class"], prov.AUTH)
        for _ in range(3):
            self.assertEqual(caller.complete("task", self.ok_parser)["provider"], "b")
        self.assertEqual(len(Path(self.counter("a")).read_text()), 1, "an auth failure disables the provider for the rest of the run")
        self.assertEqual(len(Path(self.counter("b")).read_text()), 4)
        self.assertIn("a", caller.circuits.snapshot())

    def test_unavailable_cli_falls_through_without_retry_delay(self):
        missing = Provider("ghost", {"bin": "/nonexistent/ghost-cli", "invocations": [{"argv": ["{bin}"], "stdin": True}]})
        b = self.provider("b", f"print({VALID!r})")
        caller = self.caller([missing, b])
        self.assertEqual(caller.complete("t", self.ok_parser)["provider"], "b")
        self.assertEqual(caller.sleeps, [])

    def test_only_listed_providers_are_ever_used(self):
        a = self.provider("a", "sys.exit(3)")
        caller = self.caller([a], max_attempts=2)
        with self.assertRaises(CallFailed):
            caller.complete("t", self.ok_parser)

    def test_all_providers_failing_raises_with_complete_log(self):
        a = self.provider("a", "sys.stderr.write('boom'); sys.exit(2)")
        b = self.provider("b", "sys.stderr.write('bang'); sys.exit(2)")
        caller = self.caller([a, b], max_attempts=2)
        with self.assertRaises(CallFailed) as ctx:
            caller.complete("t", self.ok_parser)
        providers_seen = [e["provider"] for e in ctx.exception.log]
        self.assertEqual(providers_seen, ["a", "a", "b", "b"])
        self.assertEqual(ctx.exception.calls, 4)

    def test_argv_style_fallback_when_stdin_style_unsupported(self):
        path = self.script("dual", f"""
            if len(sys.argv) > 1 and sys.argv[1] == "--stdin":
                sys.stderr.write("error: unknown option '--stdin'"); sys.exit(2)
            print({VALID!r} if sys.argv[1] == "-p" else "wrong")
        """)
        spec = {"bin": sys.executable, "invocations": [{"argv": ["{bin}", str(path), "--stdin"], "stdin": True},
                                                       {"argv": ["{bin}", str(path), "-p", "{prompt}"], "stdin": False}]}
        out = Provider("dual", spec).run("hello")
        self.assertTrue(out.ok, out.message)
        self.assertEqual(out.invocation, 1)

    def test_huge_prompt_is_never_placed_in_argv(self):
        path = self.script("argv", "print('never')")
        spec = {"bin": sys.executable, "invocations": [{"argv": ["{bin}", str(path), "{prompt}"], "stdin": False}]}
        out = Provider("argvonly", spec).run("x" * 200_000)
        self.assertFalse(out.ok)
        self.assertEqual(out.error, prov.CONFIG)


class RetriesAndRepair(ScriptCase):
    def test_transient_failures_retry_with_exponential_backoff_and_jitter(self):
        body = f"""
            n = int(open({self.counter('t')!r}).read()) + 1 if os.path.exists({self.counter('t')!r}) else 1
            open({self.counter('t')!r}, 'w').write(str(n))
            if n < 3:
                sys.stderr.write('503 service unavailable'); sys.exit(1)
            print({VALID!r})
        """
        caller = self.caller([self.provider("t", body)], backoff_base=4.0, max_attempts=4)
        result = caller.complete("t", self.ok_parser)
        self.assertEqual(result["calls"], 3)
        self.assertEqual(len(caller.sleeps), 2)
        self.assertTrue(2.0 <= caller.sleeps[0] <= 4.0, caller.sleeps)
        self.assertTrue(4.0 <= caller.sleeps[1] <= 8.0, caller.sleeps)

    def test_timeout_kills_process_group_and_is_classified(self):
        pidfile = self.tmp / "child.pid"
        body = f"""
            import subprocess
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
            open({str(pidfile)!r}, 'w').write(str(child.pid))
            time.sleep(120)
        """
        caller = self.caller([self.provider("slow", body)], max_attempts=1, timeout=1)
        started = time.monotonic()
        with self.assertRaises(CallFailed) as ctx:
            caller.complete("t", self.ok_parser)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(ctx.exception.log[0]["class"], prov.TIMEOUT)
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while True:
            try:
                os.kill(pid, 0)
                stat = Path(f"/proc/{pid}/stat")
                if sys.platform.startswith("linux") and (not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"):
                    break
            except (ProcessLookupError, FileNotFoundError):
                break
            if time.monotonic() >= deadline:
                self.fail(f"grandchild {pid} survived process-group termination")
            time.sleep(0.05)

    def test_malformed_output_is_repaired_once_with_correction_prompt(self):
        body = f"""
            seen = {self.counter('m')!r}
            if not os.path.exists(seen):
                open(seen, 'w').write(PROMPT)
                print('Here you go! (no json)')
            else:
                open({str(self.tmp / 'second.prompt')!r}, 'w').write(PROMPT)
                print({VALID!r})
        """
        caller = self.caller([self.provider("m", body)])
        result = caller.complete("original task", self.ok_parser)
        self.assertEqual(result["calls"], 2)
        second = (self.tmp / "second.prompt").read_text()
        self.assertIn("CORRECTION REQUEST", second)
        self.assertIn("original task", second)
        self.assertIn("no json", second)

    def test_persistently_invalid_output_fails_the_request_without_fabricating(self):
        caller = self.caller([self.provider("bad", "print('never json')")])
        with self.assertRaises(CallFailed):
            caller.complete("t", self.ok_parser)

    def test_partial_valid_result_is_kept_when_repair_cannot_fix_everything(self):
        partial = Parsed({"findings": [1]}, ["finding 2: unsupported citation"])
        caller = self.caller([self.provider("p", "print('{}')")])
        result = caller.complete("t", lambda text: partial)
        self.assertEqual(result["value"], {"findings": [1]})
        self.assertTrue(result["problems"])

    def test_output_cap_bounds_memory_and_truncated_json_is_not_accepted(self):
        body = "print('{\"findings\": [' + '\"x\",' * 200000)"
        caller = self.caller([self.provider("big", body, max_output_bytes=5000)])
        with self.assertRaises(CallFailed):
            caller.complete("t", self.ok_parser)

    def test_claude_style_envelope_usage_is_reported_only_when_present(self):
        env = json.dumps({"type": "result", "result": VALID, "usage": {"input_tokens": 11, "output_tokens": 7}, "total_cost_usd": 0.25})
        caller = self.caller([self.provider("c", f"print({env!r})")])
        result = caller.complete("t", self.ok_parser)
        self.assertEqual(result["usage"], {"input_tokens": 11, "output_tokens": 7, "cost_micro_usd": 250000})
        self.assertEqual(caller.budget.usage["cost_micro_usd"], 250000)
        plain = self.caller([self.provider("plain", f"print({VALID!r})")])
        plain.complete("t", self.ok_parser)
        self.assertEqual(plain.budget.usage["reports"], 0, "no invented usage numbers")

    def test_forge_credentials_are_removed_from_provider_environment(self):
        body = "print(json.dumps({'no_intent_found': True, 'findings': [], 'env': sorted(k for k in os.environ if 'TOKEN' in k)}))"
        with unittest.mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_x", "GITLAB_TOKEN": "glpat_x", "CUSTOM_FORGE_TOKEN": "z", "KEEP_ME": "1"}):
            provider = self.provider("e", body, env_remove=["CUSTOM_FORGE_TOKEN"])
            result = provider.run("t")
        self.assertTrue(result.ok)
        leaked = set(json.loads(result.text)["env"]) & {"GITHUB_TOKEN", "GITLAB_TOKEN", "CUSTOM_FORGE_TOKEN"}
        self.assertEqual(leaked, set())

    def test_provider_runs_in_empty_temp_directory(self):
        body = "print(json.dumps({'cwd_entries': os.listdir('.'), 'findings': [], 'no_intent_found': True}))"
        result = self.provider("w", body).run("t")
        entries = json.loads(result.text)["cwd_entries"]
        self.assertEqual(sorted(entries), ["prompt.txt"], "only the prompt file; never the repository")


class Budgets(unittest.TestCase):
    def test_budget_limits_calls_input_and_reported_cost(self):
        b = Budget(max_calls=2)
        b.reserve(10)
        b.reserve(10)
        with self.assertRaises(BudgetExhausted):
            b.reserve(10)
        b = Budget(max_input_bytes=100)
        b.reserve(60)
        with self.assertRaises(BudgetExhausted):
            b.reserve(60)
        b = Budget(max_cost_micro_usd=500)
        b.reserve(1)
        b.add_usage({"cost_micro_usd": 600})
        with self.assertRaises(BudgetExhausted):
            b.reserve(1)

    def test_circuit_trips_only_after_failures_across_distinct_requests(self):
        c = Circuits(threshold=3)
        for _ in range(5):
            c.failure("p", "nonzero_exit", "x", request="same")
        self.assertTrue(c.allow("p"), "one poisonous request must not disable the provider")
        c.failure("p", "nonzero_exit", "x", request="r2")
        c.failure("p", "nonzero_exit", "x", request="r3")
        self.assertFalse(c.allow("p"))
        c2 = Circuits(threshold=2)
        c2.failure("p", "e", request="a")
        c2.success("p")
        c2.failure("p", "e", request="b")
        self.assertTrue(c2.allow("p"), "a success resets the streak")

    def test_circuit_cooldown_reopens(self):
        clock = [1000.0]
        c = Circuits(threshold=1, cooldown=10, clock=lambda: clock[0])
        c.failure("p", "e", request="a")
        self.assertFalse(c.allow("p"))
        clock[0] += 11
        self.assertTrue(c.allow("p"))


class EngineWithProviders(TrueUpTest):
    def run_marker_history(self):
        r = self.repo
        r.commit("first good change", {"a.py": "1\n"})
        r.commit("this one always fails [ai-fail]", {"b.py": "1\n"})
        r.commit("second good change", {"c.py": "1\n"})

    def test_failed_commit_does_not_block_later_commits(self):
        self.run_marker_history()
        self.init(providers={"fake": provider_spec()})
        result = self.run_ai(ok=4)
        cov = result["coverage"]
        self.assertEqual(cov["ai_analysis"]["analyzed"], 2)
        self.assertEqual(cov["ai_analysis"]["failed"], 1)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["complete"])
        self.assertTrue(result["next"])
        failures = self.tuj("failures")
        self.assertEqual([i["stage"] for i in failures["items"]], ["analysis"])
        self.assertIn("backend crashed", failures["items"][0]["reason"])

    def test_retry_reprocesses_only_failed_items(self):
        self.run_marker_history()
        self.init(providers={"fake": provider_spec()})
        self.run_ai(ok=4)
        calls_before = len(self.calls())
        # The commit keeps failing, so retry reports incomplete again, but touches only that commit.
        again = self.tuj("retry", "--provider", "fake", "--backoff-base", "0", "--acknowledge-data-sharing", ok=4)
        new_calls = self.calls()[calls_before:]
        self.assertTrue(new_calls)
        self.assertLessEqual(len(new_calls), 6, "healthy commits are not re-analysed")
        self.assertEqual(again["coverage"]["ai_analysis"]["analyzed"], 2)

    def test_fallback_between_providers_through_cli(self):
        self.repo.commit("a good change", {"a.py": "1\n"})
        dead = script_provider(self.tmp / "dead.py")
        (self.tmp / "dead.py").write_text("import sys; sys.stderr.write('401 unauthorized'); sys.exit(1)")
        self.init(providers={"dead": dead, "fake": provider_spec()})
        result = self.tuj("run", "--provider", "dead", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["ai_analysis"]["analyzed"], 1)
        attempt = self.store().get_nested("attempts", self.repo.head(), "000001")
        self.assertEqual(attempt["findings"][0]["analysis"]["provider"], "fake")
        self.assertIn("dead", result["disabled_providers"])

    def test_outage_of_every_provider_leaves_pending_work_and_non_success(self):
        self.repo.commit("one", {"a.py": "1\n"})
        self.repo.commit("two", {"b.py": "1\n"})
        dead = script_provider(self.tmp / "dead.py")
        (self.tmp / "dead.py").write_text("import sys; sys.stderr.write('503 service unavailable'); sys.exit(1)")
        self.init(providers={"dead": dead})
        result = self.tuj("run", "--provider", "dead", "--acknowledge-data-sharing", "--backoff-base", "0", ok=4)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["coverage"]["ai_analysis"]["failed"], 2)
        self.assertEqual(result["coverage"]["inspection"]["inspected"], 2, "local inspection still succeeded and is preserved")

    def test_quota_exhaustion_persists_across_runs_until_reset(self):
        self.repo.commit("one", {"a.py": "1\n"})
        self.repo.commit("two [ai-quota]", {"b.py": "1\n"})
        self.init(providers={"fake": provider_spec()})
        first = self.run_ai(ok=4)
        self.assertEqual(first["coverage"]["ai_analysis"]["failed"], 1)
        self.assertIn("fake", self.store().circuits()["providers"], "quota circuit persisted with a cool-down")
        before = len(self.calls())
        second = self.tuj("retry", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", ok=4)
        self.assertEqual(len(self.calls()), before, "no call is made while the persisted circuit is open")
        self.assertIn("fake", second["disabled_providers"])
        listing = self.tuj("providers", "--provider", "fake", "--reset-circuits")
        self.assertTrue(listing["providers"][0]["available"])
        self.assertEqual(self.store().circuits()["providers"], {})

    def test_call_budget_stops_run_and_next_run_resumes(self):
        for i in range(4):
            self.repo.commit(f"change {i}", {f"f{i}.py": "x\n"})
        self.init(providers={"fake": provider_spec()})
        first = self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--max-ai-calls", "2", "--backoff-base", "0", ok=3)
        self.assertEqual(first["status"], "budget_exhausted")
        done = first["coverage"]["ai_analysis"]["analyzed"]
        self.assertTrue(0 < done < 4)
        self.assertEqual(first["ai_calls"], 2)
        second = self.run_ai(ok=True)
        self.assertEqual(second["status"], "complete")
        self.assertEqual(second["coverage"]["ai_analysis"]["analyzed"], 4)

    def test_max_commits_trial_run_is_resumable(self):
        for i in range(5):
            self.repo.commit(f"change {i}", {f"f{i}.py": "x\n"})
        self.init(providers={"fake": provider_spec()})
        trial = self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--max-commits", "2", "--backoff-base", "0", ok=3)
        self.assertEqual(trial["status"], "trial_limit")
        self.assertEqual(trial["coverage"]["ai_analysis"]["analyzed"], 2)
        rest = self.run_ai()
        self.assertEqual(rest["coverage"]["ai_analysis"]["analyzed"], 5)
        self.assertEqual(len(self.calls()), 5, "already-analysed commits are not sent again")

    def test_reported_cost_budget(self):
        for i in range(3):
            self.repo.commit(f"change {i}", {f"f{i}.py": "x\n"})
        env = json.dumps({"type": "result", "result": VALID, "usage": {"input_tokens": 5, "output_tokens": 5}, "total_cost_usd": 0.5})
        path = self.tmp / "costly.py"
        path.write_text(f"import sys; sys.stdin.read(); print({env!r})")
        self.init(providers={"costly": script_provider(path)})
        result = self.tuj("run", "--provider", "costly", "--acknowledge-data-sharing", "--max-cost-usd", "0.4", ok=3)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["usage_reported_by_providers"]["cost_micro_usd"], 500000)
        self.assertIn("reported", result["stop_reason"] + " reported")

    def test_oversized_prompt_is_split_and_every_part_analysed(self):
        self.repo.commit("three big files", {f"f{i}.txt": "".join(f"line {j} of file {i} " + "pad" * 20 + "\n" for j in range(30)) for i in range(3)})
        path = self.tmp / "small_window.py"
        path.write_text("import sys, subprocess\np = sys.stdin.read()\n"
                        "if len(p) > 9000:\n    sys.stderr.write('Error: prompt is too long for the context window'); sys.exit(1)\n"
                        f"r = subprocess.run([sys.executable, {str(FAKE_AI)!r}], input=p.encode(), capture_output=True)\n"
                        "sys.stdout.write(r.stdout.decode()); sys.exit(r.returncode)\n")
        cfg = self.tmp / "limits.json"
        cfg.write_text(json.dumps({"providers": {"small": script_provider(path)}, "limits": {"max_prompt_chars": 40000, "chunk_chars": 3000}}))
        self.tuj("init", "--config", str(cfg))
        result = self.tuj("run", "--provider", "small", "--acknowledge-data-sharing", "--backoff-base", "0")
        self.assertEqual(result["coverage"]["ai_analysis"]["analyzed"] + result["coverage"]["ai_analysis"]["partial"], 1, result["coverage"]["ai_analysis"])
        attempt = self.store().get_nested("attempts", self.repo.head(), "000001")
        self.assertGreater(len(attempt["chunks"]), 1, "oversize request was split into smaller chunks")


class SignalsAndInterrupts(ScriptCase):
    def test_sigterm_stops_run_cleanly_and_resume_completes(self):
        import subprocess
        for i in range(3):
            self.repo.commit(f"change {i}" + (" [ai-slow]" if i == 1 else ""), {f"f{i}.py": "x\n"})
        self.init(providers={"fake": provider_spec()})
        env = {**os.environ}
        argv = [sys.executable, str(PROJECT_TOOLS / "intentctl.py"), "trueup", "run", "--repo", str(self.repo.path), "--state-dir", str(self.state),
                "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", "--timeout", "60", "--json"]
        proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 30
        while time.time() < deadline:
            store = self.store()
            rec = store.commit(self.repo.git("rev-list", "--max-parents=0", "HEAD").stdout.decode().strip())
            if rec and rec["analysis"]["status"] == "analyzed" and self.calls() and len(self.calls()) >= 2:
                break
            time.sleep(0.2)
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 143, err.decode())
        data = json.loads(out)
        self.assertEqual(data["status"], "interrupted")
        self.assertFalse(data["complete"])
        runs = self.store().runs()
        self.assertEqual(runs[-1]["status"], "interrupted")
        status = self.tuj("status")
        self.assertFalse(status["coverage"]["complete"])
        # Resume without the slow marker interfering: the slow commit is re-attempted and killed quickly by a short timeout.
        resumed = self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0", "--timeout", "2", "--retries", "1", ok=4)
        self.assertEqual(resumed["coverage"]["ai_analysis"]["analyzed"], 2)
        self.assertEqual(resumed["coverage"]["ai_analysis"]["failed"], 1)


PROJECT_TOOLS = __import__("pathlib").Path(__file__).resolve().parents[1] / "tools"

if __name__ == "__main__":
    unittest.main()
