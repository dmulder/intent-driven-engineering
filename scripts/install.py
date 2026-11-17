#!/usr/bin/env python3
"""Install intentctl for the current user without pip, root, or network access."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

MARKER = '# intentctl user installation v1'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local')
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    prefix = args.prefix.expanduser().resolve()
    package = prefix / 'lib' / 'intent-driven-engineering'
    launcher = prefix / 'bin' / 'intentctl'
    try:
        if sys.version_info < (3, 10):
            raise RuntimeError('Python 3.10+ is required')
        if package.is_symlink() or launcher.is_symlink():
            raise RuntimeError('installation paths are symlinks; reconcile manually')
        if package.exists() and not (package / '.intentctl-install').is_file():
            raise RuntimeError(f'refusing to replace unmanaged directory {package}')
        if launcher.exists() and MARKER.encode() not in launcher.read_bytes():
            raise RuntimeError(f'refusing to replace unmanaged command {launcher}')
        if args.uninstall:
            print(f'Warning: hooks may still reference {package}. Reinstall at this prefix to run intentctl uninstall-hooks in each adopted repository.', file=sys.stderr, flush=True)
            if launcher.exists():
                launcher.unlink()
            if package.exists():
                shutil.rmtree(package)
            print('intentctl removed; repository metadata and hooks were preserved. Uninstall hooks before removing the command.')
            return 0
        project = Path(__file__).resolve().parents[1]
        package.parent.mkdir(parents=True, exist_ok=True)
        launcher.parent.mkdir(parents=True, exist_ok=True)
        # Build a complete copy before replacing a previous managed installation.
        with tempfile.TemporaryDirectory(prefix='.intentctl-', dir=package.parent) as temporary:
            payload = Path(temporary) / 'payload'
            payload.mkdir()
            shutil.copytree(project / 'tools/intentlib', payload / 'intentlib',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            shutil.copy2(project / 'tools/intentctl.py', payload / 'intentctl.py')
            shutil.copy2(project / 'LICENSE', payload / 'LICENSE')
            (payload / '.intentctl-install').write_text(MARKER + '\n')
            backup = Path(temporary) / 'previous'
            if package.exists():
                package.rename(backup)
            try:
                payload.rename(package)
            except OSError:
                if backup.exists():
                    backup.rename(package)
                raise
        # A shebang cannot quote an interpreter path containing whitespace.
        # The entry point imports its adjacent private intentlib package.
        text = ('#!/bin/sh\n' + MARKER + '\nexec '
                + shlex.join([sys.executable, str(package / 'intentctl.py')])
                + ' "$@"\n')
        descriptor, temporary = tempfile.mkstemp(prefix='.intentctl-', dir=launcher.parent)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(text)
            Path(temporary).chmod(0o755)
            os.replace(temporary, launcher)
        finally:
            Path(temporary).unlink(missing_ok=True)
        print(f'Installed {launcher}\nAdd {launcher.parent} to PATH if needed. Requires Git and Python 3.10+.')
        return 0
    except (OSError, RuntimeError) as exc:
        print(f'install: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
