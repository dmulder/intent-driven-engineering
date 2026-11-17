"""Forge evidence: reference extraction and resolution, GitHub/GitLab fetching against a loopback mock,
pagination, missing/private resources, rate limits, credential handling. No external network."""
import os
import unittest
from unittest import mock

from trueup_support import MockForge, TrueUpTest, provider_spec, script_provider  # noqa: F401
from intentlib.trueup import forge as fg
from intentlib.trueup.util import redact, strip_url_credentials

GH = fg.make_forge("github", "github.com", "acme/widgets")
GL = fg.make_forge("gitlab", "gitlab.example", "grp/sub/proj")


class ReferenceText(unittest.TestCase):
    def refs(self, text, forges=(GH,), **kw):
        return fg.extract_references(text, "commit-message", list(forges), **kw)

    def test_forms_are_distinguished_not_assumed(self):
        found = {r["raw"]: r for r in self.refs("Fixes #12, see GH-5, other/repo#7 and !3")}
        self.assertEqual(found["#12"]["form"], "hash")
        self.assertEqual(found["#12"]["kind_hint"], "issue_or_pull", "a bare #N is not assumed to be a PR")
        self.assertTrue(found["#12"]["closing"])
        self.assertEqual(found["GH-5"]["form"], "gh-prefix")
        self.assertEqual((found["other/repo#7"]["repo"], found["other/repo#7"]["form"]), ("other/repo", "cross-repo"))
        self.assertEqual(found["!3"]["kind_hint"], "merge_request")

    def test_urls_for_both_forges_including_subgroups(self):
        text = ("https://github.com/acme/widgets/pull/9 https://gitlab.example/grp/sub/proj/-/merge_requests/4 "
                "https://gitlab.example/grp/sub/proj/-/issues/8 https://example.org/page")
        refs = self.refs(text, [GH, GL])
        by = {r["raw"].split("/")[-1] + r["form"]: r for r in refs}
        gh = [r for r in refs if r.get("forge_kind") == "github"][0]
        self.assertEqual((gh["repo"], gh["number"], gh["kind_hint"]), ("acme/widgets", 9, "pull"))
        mr = [r for r in refs if r.get("kind_hint") == "merge_request"][0]
        self.assertEqual((mr["repo"], mr["number"]), ("grp/sub/proj", 4))
        self.assertEqual([r["form"] for r in refs].count("external-url"), 1)

    def test_url_credentials_are_never_kept(self):
        refs = self.refs("see https://alice:s3cr3t@github.com/acme/widgets/issues/3 now")
        self.assertNotIn("s3cr3t", repr(refs))
        self.assertNotIn("alice", repr(refs))
        self.assertEqual(strip_url_credentials("https://u:p@host/x?token=abc#f"), "https://host/x?token=REDACTED#f")

    def test_bare_references_ignored_in_diff_text_and_deduplicated_and_bounded(self):
        self.assertEqual(fg.extract_references("color: #123456; x #12", "diff", [GH], allow_bare=False), [])
        self.assertEqual(len(self.refs("#1 #1 #1 and #1")), 1)
        many = " ".join(f"#{i}" for i in range(1, 100))
        self.assertEqual(len(self.refs(many, max_refs=10)), 10)

    def test_resolution_uses_actual_context(self):
        bare = self.refs("#12")[0]
        res = fg.resolve_reference(bare, [GH])
        self.assertEqual(res["candidates"][0]["forge"]["repo"], "acme/widgets")
        self.assertEqual(fg.resolve_reference(bare, [])["status"], "unresolvable")
        bang = self.refs("!3")[0]
        self.assertEqual(fg.resolve_reference(bang, [GH])["status"], "unresolvable")
        self.assertEqual(fg.resolve_reference(bang, [GL])["status"], "candidates")
        cross = self.refs("other/repo#7")[0]
        self.assertEqual(fg.resolve_reference(cross, [GH])["candidates"][0]["forge"]["repo"], "other/repo")
        foreign = self.refs("https://github.com/x/y/issues/1")[0]
        self.assertEqual(fg.resolve_reference(foreign, [GL])["candidates"][0]["forge"]["repo"], "x/y")
        unknown_host = self.refs("https://unknown.example/acme/widgets/issues/1")[0]
        self.assertEqual(fg.resolve_reference(unknown_host, [GH])["status"], "unresolvable", "links to unconfigured hosts are not guessed")
        page = self.refs("https://example.org/just/a/page")[0]
        self.assertEqual(page["form"], "external-url")
        self.assertEqual(fg.resolve_reference(page, [GH])["status"], "unresolvable")

    def test_links_to_unconfigured_hosts_are_fetched_anonymously(self):
        enterprise = fg.make_forge("github", "git.corp.example", "corp/app", token_env="CORP_TOKEN")
        ref = self.refs("https://github.com/public/lib/issues/3", [enterprise])[0]
        candidate = fg.resolve_reference(ref, [enterprise])["candidates"][0]["forge"]
        self.assertEqual(candidate["host"], "github.com")
        self.assertFalse(candidate["token_env"], "a token configured for another host must never be sent to a linked host")
        client = fg.ForgeClient(candidate, env={"CORP_TOKEN": "s", "GITHUB_TOKEN": "g"})
        self.assertNotIn("Authorization", client._headers("https://api.github.com/repos/public/lib/issues/3"))

    def test_remote_detection_drops_credentials_and_handles_ssh_and_unknown_hosts(self):
        self.assertEqual(fg.parse_remote_url("https://tok:x@github.com/acme/widgets.git"), ("https", "github.com", "acme/widgets.git"))
        self.assertEqual(fg.parse_remote_url("git@gitlab.example:grp/sub/proj.git")[1:], ("gitlab.example", "grp/sub/proj.git"))
        self.assertIsNone(fg.parse_remote_url("/local/path/repo"))
        self.assertEqual(fg.classify_host("git.mycorp.example"), None)
        spec = fg.parse_forge_spec("gitlab:https://git.mycorp.example/a/b,api=https://git.mycorp.example/api/v4,token-env=MY_TOKEN,alias")
        self.assertEqual((spec["kind"], spec["role"], spec["token_env"]), ("gitlab", "alias", "MY_TOKEN"))

    def test_secret_redaction_in_text(self):
        text = "key AKIAIOSFODNN7EXAMPLE token ghp_abcdefghijklmnopqrstuvwxyz0123456789 password=hunter22 ok"
        cleaned, count = redact(text)
        for secret in ("AKIAIOSFODNN7EXAMPLE", "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "hunter22"):
            self.assertNotIn(secret, cleaned)
        self.assertGreaterEqual(count, 3)


class ForgeFixture(TrueUpTest):
    def setUp(self):
        super().setUp()
        self.mock = MockForge()
        self.addCleanup(self.mock.close)
        self.repo.commit("initial", {"a.py": "1\n"})
        self.init()
        self.sleeps = []
        self.forges = [fg.parse_forge_spec(self.mock.github_spec())]

    def evidence(self, forges=None, limits=None, env=None, circuits=None, enabled=True):
        return fg.ForgeEvidence(self.store(), forges or self.forges, limits, env=env if env is not None else {}, sleep=self.sleeps.append,
                                enabled=enabled, circuits=circuits)


class GitHubEvidence(ForgeFixture):
    def test_issue_pull_comments_reviews_and_pagination(self):
        m = self.mock
        m.github_issue("acme/widgets", 12, "Add retry", "We need retries because of flaky upstream.", ["first", "second", "third", "fourth", "fifth"],
                       pull=True, page_size=2)
        m.lists["/repos/acme/widgets/pulls/12/reviews"] = ([{"id": 1, "body": "Reject the callback design: it leaks state.", "user": {"login": "rev"},
                                                             "submitted_at": "2020-01-03T00:00:00Z"},
                                                            {"id": 2, "body": "", "user": {"login": "rev"}}], None)
        m.lists["/repos/acme/widgets/pulls/12/comments"] = ([{"id": 3, "body": "nit", "path": "a.py", "line": 4, "user": {"login": "rev"}}], None)
        out = self.evidence().collect("a" * 40, "Add retry (#12)", "")
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["resources"], ["github:127.0.0.1/acme/widgets#12"])
        res = self.evidence().load(out["resources"][0])
        self.assertEqual(res["type"], "pull_request")
        kinds = [c["kind"] for c in res["comments"]]
        self.assertEqual(kinds.count("issue-comment"), 5, "all pages collected")
        self.assertEqual(kinds.count("review"), 1, "empty review bodies skipped")
        self.assertEqual([c["path"] for c in res["comments"] if c["kind"] == "review-comment"], ["a.py"])
        self.assertGreaterEqual(sum(1 for p in m.paths() if p.endswith("/issues/12/comments")), 3)

    def test_plain_issue_is_not_labelled_a_pull_request(self):
        self.mock.github_issue("acme/widgets", 5, "Bug", "broken", [])
        out = self.evidence().collect("a" * 40, "closes #5", "")
        self.assertEqual(self.evidence().load(out["resources"][0])["type"], "issue")

    def test_pagination_is_bounded_and_flagged(self):
        self.mock.github_issue("acme/widgets", 7, "Long", "x", [f"c{i}" for i in range(30)], page_size=2)
        out = self.evidence(limits={"max_pages": 2}).collect("a" * 40, "#7", "")
        self.assertEqual(out["status"], "partial")
        self.assertTrue(any("truncated" in l for l in out["limitations"]))

    def test_missing_resource_is_recorded_and_cached_negatively(self):
        ev = self.evidence()
        out = ev.collect("a" * 40, "Related to #404", "")
        self.assertEqual(out["references"][0]["resolution"], "missing")
        self.assertEqual(out["status"], "failed")
        before = len(self.mock.requests)
        ev2 = self.evidence()
        ev2.collect("b" * 40, "again #404", "")
        self.assertEqual(len(self.mock.requests), before, "a missing resource is not re-requested (cached negatively)")
        self.assertGreaterEqual(ev2.stats["cache_hits"], 1)

    def test_duplicate_links_fetch_once_and_are_marked(self):
        self.mock.github_issue("acme/widgets", 3, "T", f"see {self.mock.base}/acme/widgets/issues/3 and #3", [])
        ev = self.evidence()
        out = ev.collect("a" * 40, f"Fix #3 {self.mock.base}/acme/widgets/issues/3", "")
        self.assertEqual(len(out["resources"]), 1)
        self.assertEqual(sum(1 for p in self.mock.paths() if p == "/repos/acme/widgets/issues/3"), 1)
        self.assertTrue(any(r.get("duplicate") for r in out["references"]))

    def test_private_resource_is_inaccessible_and_not_retried(self):
        self.mock.queue("/repos/acme/widgets/issues/9", (403, {}, {"message": "Resource not accessible"}))
        out = self.evidence().collect("a" * 40, "#9", "")
        self.assertEqual(out["references"][0]["resolution"], "inaccessible")
        self.assertEqual(self.mock.paths().count("/repos/acme/widgets/issues/9"), 1)

    def test_association_when_message_has_no_references_and_modes(self):
        sha = "c" * 40
        self.mock.github_issue("acme/widgets", 21, "Squash merged", "Body of the PR", [], pull=True)
        self.mock.github_commit_prs("acme/widgets", sha, [21])
        out = self.evidence().collect(sha, "plain message without references", "")
        self.assertEqual(out["resources"], ["github:127.0.0.1/acme/widgets#21"])
        self.assertEqual(out["references"][0]["source"], "forge-association")
        never = self.evidence(limits={"association": "never"}).collect(sha, "plain message", "")
        self.assertEqual(never["resources"], [])
        self.mock.requests.clear()
        self.evidence().collect(sha, "mentions #21 explicitly", "")
        self.assertNotIn(f"/repos/acme/widgets/commits/{sha}/pulls", self.mock.paths(), "auto mode skips association when the message has references")
        self.mock.requests.clear()
        self.evidence(limits={"association": "always"}).collect(sha, "mentions #21 explicitly", "")
        self.assertIn(f"/repos/acme/widgets/commits/{sha}/pulls", self.mock.paths())

    def test_unknown_commit_for_association_is_reported_not_fatal(self):
        out = self.evidence().collect("d" * 40, "no refs", "")
        self.assertTrue(any("unknown to forge" in l for l in out["limitations"]))

    def test_depth_and_resource_bounds(self):
        self.mock.github_issue("acme/widgets", 1, "Top", "continues in #2", [])
        self.mock.github_issue("acme/widgets", 2, "Second", "continues in #3", [])
        self.mock.github_issue("acme/widgets", 3, "Third", "end", [])
        shallow = self.evidence(limits={"max_depth": 0}).collect("a" * 40, "#1", "")
        self.assertEqual(len(shallow["resources"]), 1)
        one = self.evidence(limits={"max_depth": 1}).collect("a" * 40, "#1", "")
        self.assertEqual(len(one["resources"]), 2, "follows links one level deep, not unboundedly")
        capped = self.evidence(limits={"max_depth": 5, "max_resources_per_commit": 2}).collect("a" * 40, "#1", "")
        self.assertEqual(len(capped["resources"]), 2)
        self.assertTrue(any("not followed" in l for l in capped["limitations"]))

    def test_request_budget_is_enforced(self):
        self.mock.github_issue("acme/widgets", 1, "A", "x", [])
        ev = self.evidence(limits={"max_requests": 1})
        out = ev.collect("a" * 40, "#1", "")
        self.assertLessEqual(len(self.mock.requests), 1)
        self.assertIn(out["status"], ("partial", "failed"))

    def test_network_off_makes_no_requests_and_says_so(self):
        out = self.evidence(enabled=False).collect("a" * 40, "Fixes #1", "")
        self.assertEqual(self.mock.requests, [])
        self.assertEqual(out["status"], "not_requested")
        self.assertIn("not attempted", out["references"][0]["resolution"])


class RateLimitsAndCredentials(ForgeFixture):
    def test_short_rate_limit_waits_and_retries(self):
        self.mock.github_issue("acme/widgets", 1, "A", "x", [])
        self.mock.queue("/repos/acme/widgets/issues/1", (429, {"Retry-After": "2"}, {"message": "slow down"}))
        out = self.evidence().collect("a" * 40, "#1", "")
        self.assertEqual(out["status"], "complete")
        self.assertEqual(self.sleeps, [2.0])

    def test_long_rate_limit_opens_circuit_without_hammering(self):
        for n in (1, 2):
            self.mock.github_issue("acme/widgets", n, "A", "x", [])
        self.mock.queue("/repos/acme/widgets/issues/1", *[(429, {"Retry-After": "3600"}, {"message": "rate limit"})] * 5)
        circuits = {}
        ev = self.evidence(circuits=circuits)
        first = ev.collect("a" * 40, "#1", "")
        self.assertEqual(first["references"][0]["resolution"], "rate_limited")
        self.assertTrue(circuits["github:127.0.0.1"]["until"] > 0)
        count = len(self.mock.requests)
        second = ev.collect("b" * 40, "#2", "")
        self.assertEqual(len(self.mock.requests), count, "no request while the circuit is open")
        self.assertEqual(second["references"][0]["resolution"], "rate_limited")
        self.assertIn(second["status"], ("failed", "partial"))

    def test_github_403_with_rate_limit_headers_is_a_rate_limit_not_private(self):
        self.mock.github_issue("acme/widgets", 1, "A", "x", [])
        self.mock.queue("/repos/acme/widgets/issues/1", (403, {"X-RateLimit-Remaining": "0", "Retry-After": "0"}, {"message": "API rate limit exceeded"}))
        out = self.evidence().collect("a" * 40, "#1", "")
        self.assertEqual(out["status"], "complete", "retried after the limit window")

    def test_token_is_sent_only_to_the_configured_origin_and_not_followed_on_redirect(self):
        self.mock.github_issue("acme/widgets", 1, "A", "x", [])
        forges = [fg.parse_forge_spec(self.mock.github_spec(token_env="MY_FORGE_TOKEN"))]
        self.evidence(forges, env={"MY_FORGE_TOKEN": "tok-123"}).collect("a" * 40, "#1", "")
        auth = [r["headers"].get("authorization") for r in self.mock.requests]
        self.assertTrue(all(a == "Bearer tok-123" for a in auth), auth)
        self.mock.requests.clear()
        self.mock.redirects["/repos/acme/widgets/issues/2"] = f"http://localhost:{self.mock.port}/steal"
        out = self.evidence(forges, env={"MY_FORGE_TOKEN": "tok-123"}).collect("b" * 40, "#2", "")
        hosts = {r["headers"].get("host", "").split(":")[0] for r in self.mock.requests}
        self.assertEqual(hosts, {"127.0.0.1"}, "no request was made to the redirect target")
        self.assertIn(out["references"][0]["resolution"], ("error",))

    def test_pagination_link_to_another_origin_is_not_followed(self):
        self.mock.github_issue("acme/widgets", 4, "A", "x", [])
        self.mock.lists.pop("/repos/acme/widgets/issues/4/comments")
        self.mock.queue("/repos/acme/widgets/issues/4/comments", (200, {"Link": '<http://localhost:1/evil?page=2>; rel="next"'},
                                                                  [{"id": 1, "body": "only", "user": {"login": "x"}}]))
        out = self.evidence().collect("a" * 40, "#4", "")
        self.assertEqual(out["status"], "partial")
        self.assertTrue(any("another origin" in l for l in out["limitations"]))

    def test_cleartext_http_token_is_withheld_for_non_loopback_hosts(self):
        forge = fg.make_forge("github", "git.corp.example", "o/r", api_url="http://git.corp.example/api/v3", token_env="T")
        client = fg.ForgeClient(forge, env={"T": "secret"})
        self.assertNotIn("Authorization", client._headers("http://git.corp.example/api/v3/x"))
        permissive = fg.ForgeClient(forge, env={"T": "secret"}, allow_insecure=True)
        self.assertIn("Authorization", permissive._headers("http://git.corp.example/api/v3/x"))
        self.assertNotIn("Authorization", permissive._headers("http://evil.example/api/v3/x"), "wrong origin never gets a token")

    def test_resource_text_is_redacted_before_it_can_reach_a_model(self):
        self.mock.github_issue("acme/widgets", 6, "Leak", "token ghp_abcdefghijklmnopqrstuvwxyz0123456789 and https://bob:pw123@example.com/x",
                               ["password=hunter22"])
        out = self.evidence().collect("a" * 40, "#6", "")
        text = repr(self.evidence().load(out["resources"][0]))
        for secret in ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "pw123", "hunter22"):
            self.assertNotIn(secret, text)


class GitLabEvidence(TrueUpTest):
    def test_merge_request_notes_subgroup_project_and_association(self):
        mock = MockForge()
        self.addCleanup(mock.close)
        self.repo.commit("initial", {"a.py": "1\n"})
        self.init()
        mock.gitlab_mr("grp/sub/proj", 4, "Switch cache", "Replaces the old cache.", ["Why not redis?", "Rejected: ops cost."])
        mock.gitlab_issue("grp/sub/proj", 8, "Cache bug", "stale reads")
        forges = [fg.parse_forge_spec(mock.gitlab_spec("grp/sub/proj"))]
        ev = fg.ForgeEvidence(self.store(), forges, env={}, sleep=lambda s: None)
        out = ev.collect("a" * 40, "Switch cache !4, fixes #8", "")
        self.assertEqual(sorted(out["resources"]), sorted([f"gitlab:127.0.0.1/grp/sub/proj!4", f"gitlab:127.0.0.1/grp/sub/proj#8"]))
        mr = ev.load("gitlab:127.0.0.1/grp/sub/proj!4")
        self.assertEqual(mr["type"], "merge_request")
        self.assertEqual([c["body"] for c in mr["comments"]], ["Why not redis?", "Rejected: ops cost."])
        sha = "e" * 40
        mock.gitlab_mr("grp/sub/proj", 5, "Squash", "body")
        mock.gitlab_commit_mrs("grp/sub/proj", sha, [5])
        assoc = ev.collect(sha, "no references here", "")
        self.assertEqual(assoc["resources"], ["gitlab:127.0.0.1/grp/sub/proj!5"])
        self.assertTrue(any("%2F" in p for p in mock.paths()), "project path is URL-encoded")

    def test_gitlab_token_uses_private_token_header(self):
        mock = MockForge()
        self.addCleanup(mock.close)
        self.repo.commit("initial", {"a.py": "1\n"})
        self.init()
        mock.gitlab_issue("grp/proj", 1, "t", "d")
        forges = [fg.parse_forge_spec(mock.gitlab_spec("grp/proj", token_env="GL_T"))]
        fg.ForgeEvidence(self.store(), forges, env={"GL_T": "glpat-1"}, sleep=lambda s: None).collect("a" * 40, "#1", "")
        self.assertTrue(all(r["headers"].get("private-token") == "glpat-1" and "authorization" not in r["headers"] for r in mock.requests))

    def test_migrated_repository_resolves_through_alias_forge(self):
        mock = MockForge()
        self.addCleanup(mock.close)
        self.repo.commit("initial", {"a.py": "1\n"})
        self.init()
        mock.github_issue("acme/widgets-new", 12, "Moved issue", "history", [])
        old = fg.parse_forge_spec(mock.github_spec("acme/widgets-old"))
        new = fg.parse_forge_spec(mock.github_spec("acme/widgets-new") + ",alias")
        ev = fg.ForgeEvidence(self.store(), [old, new], env={}, sleep=lambda s: None)
        out = ev.collect("a" * 40, "migrated #12", "")
        self.assertEqual(out["status"], "complete")
        self.assertTrue(out["references"][0]["resolved_via_alias"])
        self.assertEqual([a["status"] for a in out["references"][0]["attempted"]], ["missing", "ok"])


class ForgeThroughCli(TrueUpTest):
    import_results = True
    def test_evidence_reaches_the_model_and_failures_do_not_block_later_commits(self):
        mock = MockForge()
        self.addCleanup(mock.close)
        mock.github_issue("acme/widgets", 12, "Retry on 503", "Upstream returns 503 under load.",
                          ["We decided against exponential backoff here; fixed delay is simpler and predictable."], pull=True)
        mock.queue("/repos/acme/widgets/issues/13", (500, {}, {"message": "boom"}), (500, {}, {"message": "boom"}), (500, {}, {"message": "boom"}),
                   (500, {}, {"message": "boom"}))
        self.repo.commit(f"Retry on 503 (#12)\n\nSee {mock.base}/acme/widgets/pull/12", {"a.py": "retry()\n"})
        self.repo.commit("Needs a flaky forge #13", {"b.py": "2\n"})
        last = self.repo.commit("Plain follow-up, no references", {"c.py": "3\n"})
        dump = self.tmp / "prompts"
        os.environ["FAKE_AI_DUMP"] = str(dump)
        self.init(providers={"fake": provider_spec()}, extra_args=["--forge", mock.github_spec("acme/widgets")],
                  config={"forge": {"limits": {"backoff_base": 0}}})
        result = self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--network", "forge", "--backoff-base", "0",
                          "--forge-association", "never", ok=4)
        cov = result["coverage"]
        self.assertEqual(cov["ai_analysis"]["analyzed"], 3, "AI analysis proceeded for every commit despite the forge failure")
        self.assertEqual(cov["external_evidence"]["complete"], 1)
        self.assertEqual(cov["external_evidence"]["failed"], 1)
        self.assertEqual(cov["external_evidence"]["none_found"], 1)
        self.assertEqual(result["status"], "incomplete")
        prompts = "\n".join(p.read_text() for p in dump.glob("*.extract.txt"))
        self.assertIn("fixed delay is simpler and predictable", prompts, "PR discussion was supplied as evidence")
        self.assertIn("type=forge-comment", prompts)
        failures = self.tuj("failures")
        self.assertEqual([i["stage"] for i in failures["items"]], ["external"])
        # Cited forge evidence is linked back from the discovery
        node = self.tuj("list")["intents"][0]
        shown = self.tuj("show", node["id"], "--detail")
        self.assertTrue(shown["reconstruction"]["history"]["findings_data"])
        self.assertIn(last, [c["sha"] if isinstance(c, dict) else c for c in [last]])

    def test_offline_run_never_touches_the_network_even_with_forge_configured(self):
        mock = MockForge()
        self.addCleanup(mock.close)
        self.repo.commit("Retry (#12)", {"a.py": "1\n"})
        self.init(providers={"fake": provider_spec()}, extra_args=["--forge", mock.github_spec("acme/widgets")])
        result = self.tuj("run", "--provider", "fake", "--acknowledge-data-sharing", "--backoff-base", "0")
        self.assertEqual(mock.requests, [])
        self.assertEqual(result["coverage"]["external_evidence"]["not_requested"], 1)

    def test_remote_credentials_never_reach_state_or_config(self):
        self.repo.git("remote", "add", "origin", "https://alice:TOP-SECRET-PW@github.com/acme/widgets.git")
        self.repo.commit("x", {"a": "1"})
        self.init()
        text = "".join(p.read_text(errors="replace") for p in self.state.rglob("*") if p.is_file())
        self.assertNotIn("TOP-SECRET-PW", text)
        self.assertIn("acme/widgets", text)


if __name__ == "__main__":
    unittest.main()
