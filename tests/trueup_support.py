"""Shared helpers for true-up tests: temp Git repositories, in-process CLI runs, fake AI provider
configuration and a local mock forge HTTP server. Nothing here uses the network beyond loopback or any paid AI."""
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.parse

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
from intentlib.trueup import cli as trueup_cli  # noqa: E402

FAKE_AI = PROJECT / "tests" / "fake_ai.py"
GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
           "GIT_TERMINAL_PROMPT": "0"}


class GitRepo:
    """A plain Git repository (intentctl is NOT installed in it) with deterministic commit times."""

    def __init__(self, path):
        self.path = Path(path)
        self.clock = 1_600_000_000
        self.path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "dev@example.invalid")
        self.git("config", "user.name", "Dev")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")

    def git(self, *args, input=None, ok=True, env=None):
        full = {**os.environ, **GIT_ENV, **(env or {})}
        result = subprocess.run(["git", *args], cwd=self.path, env=full, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if ok and result.returncode:
            raise AssertionError(f"git {args}: {result.stderr.decode()}")
        return result

    def write(self, path, data):
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)

    def commit(self, message, files=None, delete=(), date=None, allow_empty=False, branch_env=None):
        for name, data in (files or {}).items():
            self.write(name, data)
        for name in delete:
            self.git("rm", "-q", "-f", "--", name)
        self.git("add", "-A")
        self.clock += 60
        stamp = f"{date if date is not None else self.clock} +0000"
        env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp, **(branch_env or {})}
        args = ["commit", "-q", "-F", "-"]
        if allow_empty:
            args.append("--allow-empty")
        self.git(*args, input=message.encode(), env=env)
        return self.head()

    def head(self, ref="HEAD"):
        return self.git("rev-parse", ref).stdout.decode().strip()

    def state_fingerprint(self):
        """Everything a read-only tool must not change: HEAD, refs, index bytes, working-tree listing."""
        refs = self.git("for-each-ref").stdout
        index = (self.path / ".git" / "index")
        status = self.git("status", "--porcelain=v1", "-z").stdout
        return (self.head(), refs, index.read_bytes() if index.exists() else None, status,
                sorted(str(p.relative_to(self.path)) for p in self.path.rglob("*") if ".git" not in p.parts))


def provider_spec(extra_env=None, local=False, **extra):
    return {"bin": sys.executable, "local": local, "env": extra_env or {},
            "invocations": [{"argv": ["{bin}", str(FAKE_AI)], "stdin": True, "output": "stdout"}], **extra}


def script_provider(script_path, local=False, **extra):
    return {"bin": sys.executable, "local": local,
            "invocations": [{"argv": ["{bin}", str(script_path)], "stdin": True, "output": "stdout"}], **extra}


class TrueUpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.ai_log = self.tmp / "ai.log"
        patcher = mock.patch.dict(os.environ, {**GIT_ENV, "FAKE_AI_LOG": str(self.ai_log), "FAKE_AI_STATE": str(self.tmp / "ai-state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        from intentlib.trueup import forge as forge_module
        self.live_requests = []
        self.addCleanup(lambda: self.assertEqual(self.live_requests, [], "tests must never reach a live network"))
        real_request = forge_module.UrllibTransport.request

        def loopback_only(transport, method, url, headers, timeout, max_bytes):
            host = urllib.parse.urlparse(url).hostname
            if host not in ("127.0.0.1", "localhost"):
                self.live_requests.append(url)
                raise AssertionError(f"test attempted a live network request to {url}")
            return real_request(transport, method, url, headers, timeout, max_bytes)

        patch_net = mock.patch.object(forge_module.UrllibTransport, "request", loopback_only)
        patch_net.start()
        self.addCleanup(patch_net.stop)
        self.repo = GitRepo(self.tmp / "repo")
        self.state = self.tmp / "state"

    # -- CLI ---------------------------------------------------------------------------------
    def tu(self, *args, ok=True, repo=None, state=True):
        argv = list(args)
        if "--repo" not in argv:
            argv += ["--repo", str(repo or self.repo.path)]
        if state and "--state-dir" not in argv:
            argv += ["--state-dir", str(self.state)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = trueup_cli.main(argv)
            except SystemExit as exc:  # argparse errors
                code = exc.code if isinstance(exc.code, int) else 2
        if ok is True and code != 0:
            self.fail(f"trueup {args} exited {code}\nstdout: {out.getvalue()}\nstderr: {err.getvalue()}")
        if ok is not True and ok is not False and code != ok:
            self.fail(f"trueup {args} exited {code}, expected {ok}\nstdout: {out.getvalue()}\nstderr: {err.getvalue()}")
        return code, out.getvalue(), err.getvalue()

    def tuj(self, *args, ok=True, **kw):
        code, out, err = self.tu(*args, "--json", ok=ok, **kw)
        try:
            data = json.loads(out)
        except ValueError:
            self.fail(f"not JSON: {out!r} / {err!r}")
        data["_exit"], data["_stderr"] = code, err
        return data

    def init(self, providers=None, extra_args=(), config=None, **kw):
        cfg = dict(config or {})
        if providers:
            cfg["providers"] = providers
        path = self.tmp / "trueup-config.json"
        path.write_text(json.dumps(cfg))
        return self.tuj("init", "--config", str(path), *extra_args, **kw)

    def run_ai(self, *extra, provider="fake", ok=True, no_ack=False):
        args = ["run", "--provider", provider, "--backoff-base", "0", *extra]
        if not no_ack:
            args.append("--acknowledge-data-sharing")
        return self.tuj(*args, ok=ok)

    def calls(self):
        if not self.ai_log.exists():
            return []
        return [json.loads(line) for line in self.ai_log.read_text().splitlines()]

    def store(self):
        from intentlib.trueup.store import Store
        return Store.open(self.state)

    def graph(self):
        from intentlib.trueup import graph as graph_mod
        return graph_mod.load_graph(self.store())

    def small_history(self):
        """auth decision introduced, adjusted, then reverted; plus an unrelated commit."""
        r = self.repo
        c1 = r.commit("Add auth check\n\nKeep the first version simple.", {"auth.py": "def auth(t):\n    return t == 'x'\n"})
        c2 = r.commit("Use constant-time comparison for tokens\n\nPrevents timing attacks; adjusts the earlier check.",
                      {"auth.py": "import hmac\n\ndef auth(t):\n    return hmac.compare_digest(t, 'x')\n"})
        c3 = r.commit("Revert constant-time comparison\n\nThis reverts commit " + c2 + " because it breaks old clients; reverts the timing fix.",
                      {"auth.py": "def auth(t):\n    return t == 'x'\n"})
        c4 = r.commit("Add b module", {"b.py": "x = 1\n"})
        return c1, c2, c3, c4


# ---------------------------------------------------------------------------------------------
# Mock forge


class MockForge:
    """Loopback HTTP server emulating just enough of the GitHub and GitLab REST APIs."""

    def __init__(self):
        self.static = {}
        self.lists = {}
        self.queued = {}
        self.requests = []
        self.redirects = {}
        forge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                query = dict(urllib.parse.parse_qsl(parsed.query))
                with forge.lock:
                    forge.requests.append({"path": parsed.path, "query": query, "headers": {k.lower(): v for k, v in self.headers.items()}})
                    status, headers, body = forge.respond(parsed.path, query)
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.lock = threading.Lock()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def respond(self, path, query):
        for prefix, queue in self.queued.items():
            if path.startswith(prefix) and queue:
                status, headers, body = queue.pop(0)
                return status, headers, body
        if path in self.redirects:
            return 302, {"Location": self.redirects[path]}, b""
        if path in self.lists:
            items, page_size = self.lists[path]
            page = int(query.get("page", 1))
            size = page_size or int(query.get("per_page", 100))
            chunk = items[(page - 1) * size: page * size]
            headers = {}
            if page * size < len(items):
                headers["Link"] = f'<{self.base}{path}?page={page + 1}&per_page={size}>; rel="next"'
            return 200, headers, chunk
        if path in self.static:
            return 200, {}, self.static[path]
        return 404, {}, {"message": "Not Found"}

    def queue(self, prefix, *responses):
        self.queued.setdefault(prefix, []).extend(responses)

    def paths(self):
        return [r["path"] for r in self.requests]

    # GitHub conveniences -------------------------------------------------------------------
    def github_issue(self, repo, number, title, body, comments=(), pull=False, page_size=None):
        item = {"number": number, "title": title, "body": body, "state": "closed", "user": {"login": "maint"},
                "created_at": "2020-01-01T00:00:00Z", "html_url": f"https://github.com/{repo}/{'pull' if pull else 'issues'}/{number}"}
        if pull:
            item["pull_request"] = {"url": "x"}
            self.static[f"/repos/{repo}/pulls/{number}"] = {"merged": True, "merge_commit_sha": None}
            self.lists[f"/repos/{repo}/pulls/{number}/reviews"] = ([], None)
            self.lists[f"/repos/{repo}/pulls/{number}/comments"] = ([], None)
        self.static[f"/repos/{repo}/issues/{number}"] = item
        self.lists[f"/repos/{repo}/issues/{number}/comments"] = (
            [{"id": 100 + i, "body": c, "user": {"login": "reviewer"}, "created_at": "2020-01-02T00:00:00Z",
              "html_url": f"https://github.com/{repo}/issues/{number}#c{i}"} for i, c in enumerate(comments)], page_size)

    def github_commit_prs(self, repo, sha, numbers):
        self.lists[f"/repos/{repo}/commits/{sha}/pulls"] = ([{"number": n} for n in numbers], None)

    # GitLab conveniences -------------------------------------------------------------------
    def gitlab_mr(self, project, iid, title, description, notes=()):
        quoted = urllib.parse.quote(project, safe="")
        self.static[f"/api/v4/projects/{quoted}/merge_requests/{iid}"] = {
            "iid": iid, "title": title, "description": description, "state": "merged", "author": {"username": "maint"},
            "created_at": "2020-01-01T00:00:00Z", "web_url": f"https://gitlab.example/{project}/-/merge_requests/{iid}"}
        self.lists[f"/api/v4/projects/{quoted}/merge_requests/{iid}/notes"] = (
            [{"id": 200 + i, "body": n, "author": {"username": "reviewer"}, "created_at": "2020-01-02T00:00:00Z", "system": False}
             for i, n in enumerate(notes)], None)

    def gitlab_issue(self, project, iid, title, description):
        quoted = urllib.parse.quote(project, safe="")
        self.static[f"/api/v4/projects/{quoted}/issues/{iid}"] = {
            "iid": iid, "title": title, "description": description, "state": "closed", "author": {"username": "maint"},
            "created_at": "2020-01-01T00:00:00Z", "web_url": f"https://gitlab.example/{project}/-/issues/{iid}"}
        self.lists[f"/api/v4/projects/{quoted}/issues/{iid}/notes"] = ([], None)

    def gitlab_commit_mrs(self, project, sha, iids):
        quoted = urllib.parse.quote(project, safe="")
        self.lists[f"/api/v4/projects/{quoted}/repository/commits/{sha}/merge_requests"] = ([{"iid": i} for i in iids], None)

    def github_spec(self, repo="acme/widgets", token_env=None):
        spec = f"github:{self.base}/{repo},api={self.base}"
        return spec + (f",token-env={token_env}" if token_env else "")

    def gitlab_spec(self, project="grp/proj", token_env=None):
        spec = f"gitlab:{self.base}/{project},api={self.base}/api/v4"
        return spec + (f",token-env={token_env}" if token_env else "")
