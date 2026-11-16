#!/usr/bin/env python3
"""Create and exercise a disposable real Git repository. No third-party modules."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
INITIAL = """def authenticate(user, token):
    if token.valid and not user.locked:
        return True
    return False
"""
REFACTORED = """def permitted(user, token):
    return token.valid and not user.locked


def authenticate(user, token):
    return permitted(user, token)
"""


def demonstrate(destination):
    root = Path(destination).resolve()
    if root.exists():
        raise SystemExit("destination must not exist (demo never overwrites a repository)")
    root.mkdir(parents=True)
    shutil.copytree(PROJECT / "tools", root / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "PYTHONDONTWRITEBYTECODE": "1"}

    def run(*argv, expect=0):
        result = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
        if (expect == 0 and result.returncode) or (expect != 0 and not result.returncode):
            raise RuntimeError(f"{argv}: {result.stdout}\n{result.stderr}")
        return result

    def git(*args, **kwargs):
        return run("git", *args, **kwargs)

    def ctl(*args, **kwargs):
        return json.loads(run(sys.executable, "tools/intentctl.py", *args, "--json", **kwargs).stdout)

    def capture(key):
        value = ctl("capture", "--key", key, "--description", key, "--rationale", "Demonstrate explicit intent evolution")
        file = root / value["draft"]
        draft = json.loads(file.read_text())
        draft["event"]["provenance"] = "unknown"
        # Drafts are intentionally human editable, unlike finalized records.
        file.write_text(json.dumps(draft, indent=2) + "\n")

    def finish(key):
        ctl("finalize", key)
        git("add", ".intent")
        ctl("validate", "--staged")
        git("commit", "-qm", key)

    def introduce(key, title, requirement):
        capture(key)
        i = ctl("intent-create", "--event", key, "--key", key, "--title", title,
                "--description", requirement, "--rationale", "Authentication must enforce independent policies",
                "--requirement", requirement)["id"]
        data = (root / "auth.py").read_bytes()
        start = data.index(b"token.valid")
        end = data.index(b":", start)
        b = ctl("bind", "--event", key, "--key", key, "--intent", i, "--path", "auth.py",
                "--start", str(start), "--end", str(end), "--rationale", "Condition implements this policy")["id"]
        ctl("declare", "--event", key, "--intent", i, "--binding", b, "--action", "introduce")
        finish(key)
        return i, b

    def preserve(key, pair, revision=None):
        i, b = pair
        data = (root / "auth.py").read_bytes()
        start = data.index(b"token.valid")
        end = data.index(b"\n", start)
        args = ["rebind", b, "--event", key, "--path", "auth.py", "--start", str(start), "--end", str(end),
                "--rationale", "Explicitly preserve policy in helper implementation"]
        if revision:
            args += ["--intent-revision", str(revision)]
        ctl(*args)
        ctl("declare", "--event", key, "--intent", i, "--binding", b, "--action", "preserve")

    git("init", "-q")
    git("config", "user.name", "Intent demo")
    git("config", "user.email", "intent-demo@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.autocrlf", "false")
    (root / "auth.py").write_text(INITIAL)
    (root / ".gitignore").write_text("__pycache__/\n.intent/drafts/\n")
    git("add", ".")
    ctl("init")
    git("add", ".intent")
    git("commit", "-qm", "Explicit unknown baseline")
    tokens = introduce("A-valid-tokens", "Token validity", "Accept authentication only with valid tokens")
    print("A: introduced token-validity intent")
    locks = introduce("B-account-lockout", "Account lockout", "Reject authentication for locked accounts")
    applicable = ctl("explain", "auth.py:2")["applicable"]
    assert len(applicable) == 2
    print("B: two independent intents overlap on auth.py:2")
    initial_commit = git("rev-parse", "HEAD").stdout.strip()
    (root / "auth.py").write_text(REFACTORED)
    git("add", "auth.py")
    capture("C-helper-refactor")
    preserve("C-helper-refactor", tokens)
    preserve("C-helper-refactor", locks)
    finish("C-helper-refactor")
    print("C: helper refactor explicitly preserved both intents")
    before = git("hash-object", "auth.py").stdout
    capture("D-token-policy-revision")
    ctl("intent-revise", tokens[0], "--event", "D-token-policy-revision", "--requirement", "Reject valid tokens whose expiry has passed")
    ctl("declare", "--event", "D-token-policy-revision", "--intent", tokens[0], "--action", "modify")
    finish("D-token-policy-revision")
    assert git("hash-object", "auth.py").stdout == before
    assert ctl("impact", tokens[0])["bindings"][0]["needs_reassessment"]
    print("D: intent revision 2 changed no source; token binding needs reassessment")
    (root / "auth.py").write_text(REFACTORED.replace("token.valid", "token.valid and not token.expired"))
    git("add", "auth.py")
    capture("E-reject-expired")
    preserve("E-reject-expired", tokens, 2)
    preserve("E-reject-expired", locks)
    finish("E-reject-expired")
    assert not ctl("status")["reassessment"]
    print("E: implementation updated; both bindings revised; lockout intent unchanged")
    history = ctl("history", "auth.py:2")
    assert len(history["events"]) == 5
    assert len(ctl("explain", "auth.py:2", "--at", initial_commit)["applicable"]) == 2
    print("F: history reconstructed 5 events and token intent revisions 1 and 2")
    (root / "auth.py").write_text((root / "auth.py").read_text().replace("return token.valid", "return bool(token.valid").replace("not user.locked\n", "not user.locked)\n"))
    git("add", "auth.py")
    capture("GH-explicit-accounting")
    preserve("GH-explicit-accounting", tokens, 2)
    failure = ctl("finalize", "GH-explicit-accounting", expect=1)
    assert "MISSING_TRANSITION" in str(failure)
    rejected = git("commit", "-qm", "Must be rejected", expect=1)
    assert "UNCOVERED_CHANGE" in rejected.stdout + rejected.stderr
    print("G: missing lockout declaration rejected; Git hook also rejected commit")
    preserve("GH-explicit-accounting", locks)
    finish("GH-explicit-accounting")
    print("H: supplied missing declaration; hook accepted commit")
    ctl("validate", "--range", initial_commit + "..HEAD")
    report = {"repository": str(root), "token_intent": tokens[0], "lockout_intent": locks[0],
              "initial_commit": initial_commit, "final_commit": git("rev-parse", "HEAD").stdout.strip(),
              "events": len(ctl("history", "auth.py:2")["events"])}
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", help="new directory for disposable demo Git repository")
    demonstrate(parser.parse_args().destination)
