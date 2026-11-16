import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
from intentlib.cli import draft_path
from intentlib.core import canonical, loads, seal, write_json


class RepoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(PROJECT / "tools", self.root / "tools", ignore=shutil.ignore_patterns("__pycache__"))
        self.git("init", "-q")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("config", "user.name", "Tests")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.write(".gitignore", "__pycache__/\n.intent/drafts/\n.intent/cache/\n")
        self.write("auth.py", (PROJECT / "tests/fixtures/auth.py").read_bytes())
        self.git("add", ".")
        self.ctl("init")
        self.git("add", ".intent")
        self.commit("unknown baseline")

    def run_cmd(self, argv, ok=True, input=None):
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(argv, cwd=self.root, env=env, input=input,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if ok and result.returncode:
            self.fail(f"{argv}: {result.stdout.decode()} {result.stderr.decode()}")
        return result

    def git(self, *args, ok=True, input=None):
        return self.run_cmd(["git", *args], ok, input)

    def ctl(self, *args, ok=True):
        result = self.run_cmd([sys.executable, "tools/intentctl.py", *args, "--json"], ok)
        try:
            return json.loads(result.stdout)
        except ValueError:
            self.fail(result.stdout.decode() + result.stderr.decode())

    def write(self, path, data):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data.encode() if isinstance(data, str) else data)

    def read(self, path):
        return loads((self.root / path).read_bytes())

    def commit(self, message):
        return self.git("commit", "-qm", message)

    def capture(self, key, provenance="unknown"):
        result = self.ctl("capture", "--key", key, "--description", key, "--rationale", "explicit test rationale")
        path = result["draft"]
        draft = self.read(path)
        draft["event"]["provenance"] = provenance
        write_json(self.root / path, draft)
        return path

    def introduce(self, key, path="auth.py", start=0, end=None, event=None):
        event = event or key
        if not draft_path(self.root, event).exists():
            self.capture(event)
        i = self.ctl("intent-create", "--event", event, "--key", key, "--title", key,
                     "--description", "Required behavior " + key, "--rationale", "Explicit rationale", "--requirement", "Keep behavior")["id"]
        args = ["bind", "--event", event, "--key", key, "--intent", i, "--path", path, "--start", str(start), "--rationale", "Implements behavior"]
        if end is not None:
            args += ["--end", str(end)]
        b = self.ctl(*args)["id"]
        self.ctl("declare", "--event", event, "--intent", i, "--binding", b, "--action", "introduce")
        return i, b

    def cover_new_bytes(self, event, key=None):
        """Declare intents for every after-path that introduces bytes in the draft."""
        draft = self.read(str(draft_path(self.root, event).relative_to(self.root)))
        key = key or (event + "-cover")
        covered = []
        for index, change in enumerate(draft["event"]["changes"]):
            if change["after"] is None:
                continue
            if not any(a < z for a, z in (interval["after"] for interval in change["intervals"])):
                continue
            covered.append(self.introduce(f"{key}-{index}", path=change["path"], event=event))
        return covered

    def finish(self, event, commit=True, cover=False):
        if cover:
            draft = self.read(str(draft_path(self.root, event).relative_to(self.root)))
            for req in draft.get("required_declarations", []):
                self.preserve(event, req["intent_id"], req["binding_id"], path=req["target"]["path"])
            self.cover_new_bytes(event)
        self.ctl("finalize", event)
        self.git("add", ".intent")
        self.ctl("validate", "--staged")
        if commit:
            self.commit(event)

    def bound(self, key="tokens", **kwargs):
        i, b = self.introduce(key, **kwargs)
        self.finish(key)
        return i, b

    def preserve(self, key, i, b, path="auth.py", action="preserve", **kwargs):
        args = ["rebind", b, "--event", key, "--rationale", "Explicit continuity", "--path", path]
        for name, value in kwargs.items():
            args += ["--" + name.replace("_", "-"), str(value)]
        self.ctl(*args)
        self.ctl("declare", "--event", key, "--intent", i, "--binding", b, "--action", action)
