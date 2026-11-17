"""Regression coverage for PR review: untrusted replies, bounded I/O and safe recovery."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from trueup_support import GitRepo, MockForge, TrueUpTest, provider_spec
from test_trueup_providers import ScriptCase
import test_trueup_graph as graph_tests
from intentlib.trueup import analysis, forge, gitscan, graph, providers
from intentlib.trueup.store import Store
from intentlib.trueup.util import TrueUpError


class ReplyValidation(unittest.TestCase):
    def test_nonobject_replies_and_unhashable_identifiers(self):
        parsers = [analysis.make_consolidation_parser([{}]), graph.make_link_parser(graph_tests.items(), [{"id": "f"}], [{"id": "n"}])]
        for parser in parsers:
            for value in [[], 7, True, "text", None]:
                with self.subTest(value=value):
                    result = parser(json.dumps(value))
                    self.assertIsNone(result.value)
                    self.assertIsNotNone(result.kind)
        finding = graph_tests.ModelOutputValidation().finding()
        bad = {**finding, "evidence": [{"ref": []}]}
        result = analysis.validate_findings({"findings": [bad, finding]}, graph_tests.items(), [])
        self.assertEqual(len(result.value["findings"]), 1)
        result = parsers[0](json.dumps({"groups": [{"members": [[], {}, "K1"]}]}))
        self.assertEqual(result.value[0]["members"], ["K1"])
        result = parsers[1](json.dumps({"links": [{"finding": [], "target": {}},
                          {"finding": "F1", "target": "N1", "relation": "related", "evidence": [{"ref": []}]}]}))
        self.assertEqual(result.value, [])

    def test_large_path_evidence_splits_with_shared_commit_context(self):
        c = {"ref": "C0", "text": "c" * 1000}
        p = {"ref": "P0", "text": "path.py\n" * 10000}
        bundle = analysis.make_bundles([c, p], analysis.DEFAULT_LIMITS)[0]
        split = analysis.split_bundle(bundle)
        self.assertEqual(len(split), 2)
        self.assertTrue(all(b[0] == c for b in split))
        self.assertEqual("".join(b[1]["text"] for b in split), p["text"])

    def test_link_message_redacted_before_prompt_and_quote_validation(self):
        evidence = graph.link_items([], [], "password=hunter22")
        self.assertNotIn("hunter22", graph.link_prompt(evidence))
        self.assertNotIn("hunter22", repr(evidence))

    def test_raw_diff_requires_all_path_delimiters(self):
        head = b":100644 100644 " + b"a" * 40 + b" " + b"b" * 40
        for status, paths in [(b"M", b"very-long.py\0"), (b"R100", b"old.py\0new.py\0"), (b"C100", b"old.py\0new.py\0")]:
            data = head + b" " + status + b"\0" + paths
            self.assertEqual(len(gitscan.parse_raw_diff(data)), 1)
            for cut in range(len(paths)):
                self.assertEqual(gitscan.parse_raw_diff(data[:len(data)-len(paths)+cut]), [])


class GitIsolation(TrueUpTest):
    def test_inherited_location_overrides_do_not_escape_temp_repository(self):
        other = GitRepo(self.tmp / "other")
        sha_other = other.commit("other", {"outside": "unrelated"})
        sha = self.repo.commit("inside", {"inside": "ours"})
        env = {"GIT_DIR": str(other.path / ".git"), "GIT_WORK_TREE": str(other.path),
               "GIT_COMMON_DIR": str(other.path / ".git"), "GIT_INDEX_FILE": str(other.path / ".git/index"),
               "GIT_OBJECT_DIRECTORY": str(other.path / ".git/objects"),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other.path / ".git/objects")}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(gitscan.Repo(self.repo.path).run("rev-parse", "HEAD").decode().strip(), sha)
            self.assertEqual(self.repo.head(), sha)
            self.assertFalse(set(env) & set(gitscan.git_env()))
        self.assertEqual(other.head(), sha_other)

    def test_capped_commands_distinguish_failure_timeout_and_cap(self):
        self.repo.commit("initial", {"x": "x" * 1000})
        repo = gitscan.Repo(self.repo.path)
        with self.assertRaises(gitscan.GitFailure):
            repo.run_capped("cat-file", "blob", "HEAD:missing", cap=100)
        with self.assertRaises(gitscan.GitFailure):
            repo.run_capped("-c", "alias.pause=!sleep 2", "pause", cap=100, timeout=0.05)
        data, truncated = repo.run_capped("cat-file", "blob", "HEAD:x", cap=20)
        self.assertEqual(data, b"x" * 20)
        self.assertTrue(truncated)

    def test_blame_does_not_invoke_textconv(self):
        marker = self.tmp / "executed"
        script = self.tmp / "evil.sh"
        script.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat "$1"\n')
        script.chmod(0o755)
        sha = self.repo.commit("data", {".gitattributes": "*.dat diff=evil\n", "x.dat": "hello\n"})
        self.repo.git("config", "diff.evil.textconv", str(script))
        result = gitscan.blame_lines(gitscan.Repo(self.repo.path), "HEAD", "x.dat")
        self.assertEqual(result, {1: sha})
        self.assertFalse(marker.exists())

    def test_store_initialization_rechecks_under_lock_and_releases(self):
        repo = gitscan.Repo(self.repo.path)
        acquire = Store.acquire
        def race(store):
            acquire(store)
            (store.path / "meta.json").write_text('{"sentinel":true}')
        with mock.patch.object(Store, "acquire", race):
            with self.assertRaises(TrueUpError):
                Store.create(self.state, repo, {"overwrite": True})
        self.assertEqual(json.loads((self.state / "meta.json").read_text()), {"sentinel": True})
        self.assertFalse((self.state / "config.json").exists())
        store = Store(self.state)
        store.acquire()
        store.release()

    def test_audit_append_after_torn_line_preserves_new_event(self):
        store = Store(self.state)
        store.acquire()
        self.addCleanup(store.release)
        (self.state / "events.jsonl").write_bytes(b'{"torn":')
        store.event("recovered", value=1)
        self.assertEqual(store.events()[0]["kind"], "corrupt-log-line")
        self.assertEqual(store.events()[1]["kind"], "recovered")

    def test_verify_integrity_failure_has_nonzero_exit(self):
        sha = self.repo.commit("initial", {"x": "x"})
        self.init()
        self.tu("run", "--no-ai")
        store = self.store()
        rec = store.commit(sha)
        rec["analysis"] = {"status": "analyzed"}
        store.put_commit(rec)
        self.assertFalse(self.tuj("verify", ok=1)["ok"])


class ProviderBounds(ScriptCase):
    def file_provider(self, text, limit):
        script = self.script("file", f"open(sys.argv[1], 'w', encoding='utf-8').write({text!r})")
        return providers.Provider("file", {"bin": sys.executable, "max_output_bytes": limit,
            "invocations": [{"argv": ["{bin}", str(script), "{output_file}"], "stdin": True, "output": "file"}]})

    def test_output_file_limit_is_bytes_and_applies_to_codex_style(self):
        for text, limit, expected in [("hello", 5, True), ("é" * 10, 10, False), ("x" * 100000, 50, False)]:
            result = self.file_provider(text, limit).run("prompt")
            self.assertEqual(result.ok, expected)
            if not expected:
                self.assertTrue(result.truncated)

    def test_error_envelope_usage_counts_toward_budget(self):
        provider = self.provider("charged", "print(json.dumps({'is_error':True, 'result':'503 unavailable', 'total_cost_usd':0.25}))")
        result = provider.run("prompt")
        self.assertFalse(result.ok)
        self.assertEqual(result.usage["cost_micro_usd"], 250000)
        budget = providers.Budget(max_cost_micro_usd=200000)
        caller = self.caller([provider], budget=budget, max_attempts=3)
        with self.assertRaises(providers.BudgetExhausted):
            caller.complete("prompt", self.ok_parser)
        self.assertEqual(caller.calls, 1)

    def test_partial_findings_survive_transient_repair_exhaustion(self):
        provider = self.provider("partial", f"""
            path = {self.counter('partial')!r}
            if os.path.exists(path):
                sys.stderr.write('503 unavailable'); sys.exit(1)
            open(path, 'w').write('1')
            print('partial')
        """)
        caller = self.caller([provider], max_attempts=2, max_repairs=1)
        result = caller.complete("prompt", lambda _: providers.Parsed({"findings": ["supported"]}, ["bad finding dropped"]))
        self.assertEqual(result["value"]["findings"], ["supported"])


class ForgeRecovery(unittest.TestCase):
    def test_secondary_network_errors_stay_retryable_and_truncation_is_partial(self):
        gh = {"kind": "github", "host": "github.com", "scheme": "https", "repo": "acme/widgets"}
        gl = {"kind": "gitlab", "host": "gitlab.example", "scheme": "https", "repo": "acme/widgets"}
        limits = {**forge.DEFAULT_LIMITS, "max_comment_chars": 5}
        for desc, fail in [(gh, "details"), (gh, "discussion"), (gl, "discussion")]:
            client = mock.Mock()
            def get_json(path):
                if '/pulls/' in path and fail == 'details':
                    raise forge.NetworkError("disconnected")
                return {"body": "ok", "description": "ok", "pull_request": {}}, {}
            client.get_json.side_effect = get_json
            if fail == "discussion":
                client.paginate.side_effect = forge.NetworkError("disconnected")
            else:
                client.paginate.return_value = ([], False, None)
            result = forge.fetch_resource(client, {"forge": desc, "number": 1, "kind_hint": "issue"}, limits)
            self.assertEqual(result["status"], "partial")
            self.assertTrue(result["limits"]["retryable"])
        client = mock.Mock()
        client.get_json.return_value = ({"body": "ok"}, {})
        client.paginate.return_value = ([{"id": 1, "body": "a long comment"}], False, None)
        result = forge.fetch_resource(client, {"forge": gh, "number": 1, "kind_hint": "issue"}, limits)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["comments"][0]["truncated"])


class PipelineRecovery(TrueUpTest):
    def test_retry_of_highest_commit_never_links_to_itself_or_duplicates_nodes(self):
        self.repo.commit("Add auth", {"auth.py": "x=1\n"})
        sha = self.repo.commit("adjusts auth [ai-flaky]", {"auth.py": "x=2\n"})
        self.init(providers={"fake": provider_spec()})
        self.run_ai("--retries", "1", ok=4)
        self.tuj("retry", "--provider", "fake", "--acknowledge-data-sharing", "--retries", "1", "--backoff-base", "0", ok=4)
        final = self.tuj("retry", "--provider", "fake", "--acknowledge-data-sharing", "--retries", "1", "--backoff-base", "0")
        self.assertTrue(final["complete"])
        record = self.store().get("links", sha)
        self.assertEqual(len(record["candidates"]), 1)
        self.assertEqual(len(self.graph().nodes), 1)

    def test_subject_secret_is_not_persisted(self):
        sha = self.repo.commit("Configure password=hunter22", {"x": "ok"})
        self.init(providers={"fake": provider_spec()})
        self.run_ai()
        store = self.store()
        self.assertNotIn("hunter22", repr(store.get_nested("attempts", sha, "000001")))
        self.assertNotIn("hunter22", repr(self.graph().nodes))


class ForgeAliasContext(TrueUpTest):
    def test_linked_references_use_the_successful_alias_api(self):
        primary, alias = MockForge(), MockForge()
        self.addCleanup(primary.close)
        self.addCleanup(alias.close)
        alias.github_issue('acme/new', 1, 'Moved issue', 'See #2')
        alias.github_issue('acme/new', 2, 'Correct issue', 'alias content')
        descriptors = [forge.parse_forge_spec(primary.github_spec('acme/old')),
                       forge.parse_forge_spec(alias.github_spec('acme/new').replace('127.0.0.1', 'localhost'))]
        descriptors[1]['role'] = 'alias'
        evidence = forge.ForgeEvidence(Store(self.state), descriptors, limits={'association': 'off', 'max_depth': 1}, allow_insecure=True)
        result = evidence.collect('a'*40, 'See #1', '')
        self.assertEqual(result['status'], 'complete')
        self.assertIn('/repos/acme/new/issues/2', alias.paths())
        self.assertNotIn('/repos/acme/new/issues/2', primary.paths())
        self.assertEqual(len(result['resources']), 2)


class SignatureOutput(TrueUpTest):
    def test_merge_summary_suppresses_configured_signature_output(self):
        import shutil
        if not shutil.which('ssh-keygen'):
            self.skipTest('ssh-keygen unavailable')
        self.repo.commit('root', {'x': 'root'})
        self.repo.git('checkout', '-qb', 'side')
        key = self.tmp / 'signing-key'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
        self.repo.git('config', 'gpg.format', 'ssh')
        self.repo.git('config', 'user.signingkey', str(key))
        self.repo.git('config', 'commit.gpgsign', 'true')
        signed = self.repo.commit('signed side', {'side': 'signed'})
        self.repo.git('config', 'commit.gpgsign', 'false')
        self.repo.git('checkout', '-q', 'main')
        self.repo.commit('main', {'main': 'main'})
        self.repo.git('merge', '--no-ff', '-qm', 'merge', 'side')
        self.repo.git('config', 'log.showSignature', 'true')
        limits = analysis.DEFAULT_LIMITS
        info = gitscan.inspect_commit(gitscan.Repo(self.repo.path), self.repo.head(), limits)
        self.assertIn(signed, [item['sha'] for item in info['merge']['merged_commits']])
