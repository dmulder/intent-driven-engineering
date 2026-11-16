#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
command -v git >/dev/null

python3 -m compileall -q tools tests examples
python3 tools/intentctl.py status --json >/dev/null
