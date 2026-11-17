"""Validate the introduced history, including pushes that replace a branch."""
import os
from pathlib import Path
import re
import subprocess
import sys


def validation_command(root, base, event, default_branch):
    command = [sys.executable, "tools/intentctl.py", "validate"]
    # Preserve the branch-creation policy: do not audit accepted history with a newer engine.
    if not base or set(base) == {"0"}:
        return command + ["--commit", "HEAD"]
    if not re.fullmatch(r"[0-9a-fA-F]{40}", base):
        raise ValueError("BASE_SHA must be a full commit SHA")

    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=root, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=check)

    if event == "push":
        available = git("cat-file", "-e", base + "^{commit}", check=False).returncode == 0
        ancestor = available and git("merge-base", "--is-ancestor", base, "HEAD", check=False).returncode == 0
        if not ancestor:
            # A force push may make event.before unreachable. Checkout fetches all
            # branch heads; validate the rewritten branch against accepted default history.
            trusted = "refs/remotes/origin/" + default_branch
            if git("rev-parse", "--verify", trusted + "^{commit}", check=False).returncode:
                raise ValueError("default branch is unavailable; fetch full branch history before validation")
            if git("rev-parse", trusted).stdout == git("rev-parse", "HEAD").stdout:
                raise ValueError("cannot infer a trusted base for a default-branch rewrite; validate an explicit base")
            base = git("merge-base", trusted, "HEAD").stdout.strip()
    return command + ["--range", base + "..HEAD"]


def main():
    try:
        command = validation_command(Path.cwd(), os.environ.get("BASE_SHA", ""),
                                     os.environ.get("EVENT_NAME", ""), os.environ["DEFAULT_BRANCH"])
        return subprocess.run(command).returncode
    except (ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"Cannot select validation history: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
