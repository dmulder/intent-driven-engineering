"""Regression scenarios from PR #3's audit, deployment, and migration review."""
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
from unittest.mock import patch

from support import PROJECT, RepoTest


class AuditReviewTests(RepoTest):
    def test_managed_protocol_update_accepts_crlf(self):
        self.write('INTENT.md', b'<!-- intentctl:begin -->\r\nOld protocol\r\n<!-- intentctl:end -->\r\n')
        self.assertIn('already exists', str(self.ctl('deploy', ok=False)))
        self.ctl('deploy', '--update')
        self.assertEqual((self.root / 'INTENT.md').read_bytes(), (PROJECT / 'tools/intentlib/protocol.md').read_bytes())

    def test_invalid_working_metadata_preserves_history_and_reconciliation(self):
        base = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.write('auth.py', 'human change without an event\n')
        self.git('add', 'auth.py')
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'unaccounted commit')
        bad = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        for path, data, code in [('.intent/schema/binding.schema.json', '{}\n', 'SCHEMA_DEFINITION'),
                                 ('.intent/manifests/current.json', 'not JSON\n', 'PARSE')]:
            with self.subTest(path=path):
                original = (self.root / path).read_bytes()
                self.write(path, data)
                audit = self.ctl('audit', '--since', base, ok=False)
                self.assertTrue(any(e.get('commit') == bad and e['code'] == 'UNCOVERED_CHANGE' for e in audit['errors']))
                self.assertTrue(any(e['code'] == code and 'commit' not in e for e in audit['errors']))
                self.assertFalse(audit['derived_state_available'])
                packet = self.ctl('reconcile', '--since', base, ok=False)
                self.assertEqual(packet['audit']['errors'], audit['errors'])
                self.assertTrue(packet['task'])
                self.write(path, original)

    def test_audit_pins_base_and_rejects_a_commit_landing_during_validation(self):
        from intentlib.adoption import audit
        from intentlib.cli import run_validation
        from intentlib.core import IntentError
        tip = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.write('auth.py', 'unaccounted change committed during the audit\n')
        self.git('add', 'auth.py')
        bases, cache = [], {}
        def commit_then_validate(root, args):
            bases.append(args.base)
            self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'concurrent commit')
            return run_validation(root, args)
        with patch('intentlib.cli.run_validation', side_effect=commit_then_validate):
            with self.assertRaisesRegex(IntentError, 'repository changed while auditing'):
                audit(self.root, cache=cache)
        self.assertEqual(bases, [tip])
        self.assertNotIn('working', cache)

    def test_cached_audit_checks_both_head_and_working_snapshot(self):
        from intentlib.adoption import audit, review_state
        from intentlib.core import IntentError
        original = (self.root / 'auth.py').read_bytes()
        for change in ('head', 'working'):
            with self.subTest(change=change):
                self.write('auth.py', original)
                cache = {}
                audit(self.root, cache=cache)
                def change_after_snapshot(root, entries):
                    state = review_state(root, entries)
                    if change == 'head':
                        self.git('commit', '--allow-empty', '-qm', 'concurrent HEAD movement')
                    else:
                        self.write('auth.py', 'concurrent working edit\n')
                    return state
                with patch('intentlib.adoption.review_state', side_effect=change_after_snapshot):
                    with self.assertRaisesRegex(IntentError, 'repository changed while auditing'):
                        audit(self.root, cache=cache)
        self.write('auth.py', original)

    def test_non_utf8_filename_is_an_error_and_watcher_recovers(self):
        invalid = os.fsencode(self.root) + b'/bad-\xff'
        with open(invalid, 'wb') as stream:
            stream.write(b'content is not ingested')
        for command in (['audit'], ['watch', '--once']):
            result = self.run_cmd([sys.executable, 'tools/intentctl.py', *command, '--json'], ok=False)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(b'Traceback', result.stderr)
            self.assertIn('non-UTF-8 filenames', str(json.loads(result.stdout)))
        env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
               'PYTHONDONTWRITEBYTECODE': '1'}
        with subprocess.Popen([sys.executable, 'tools/intentctl.py', 'watch', '--interval', '0.05', '--json'],
                              cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            def read_state():
                ready, _, _ = select.select([proc.stdout], [], [], 20)
                self.assertTrue(ready, 'watch did not emit a state')
                line = proc.stdout.readline()
                self.assertTrue(line, 'watch exited before emitting a state')
                return json.loads(line)
            try:
                self.assertIn('non-UTF-8 filenames', str(read_state()))
                os.unlink(invalid)
                recovered = read_state()
                self.assertFalse(recovered['errors'])
                self.assertIsNone(proc.poll())
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            self.assertNotIn(b'Traceback', proc.stderr.read())

    def test_non_utf8_rename_source_is_an_operational_error(self):
        invalid = os.fsencode(self.root) + b'/old-\xff'
        with open(invalid, 'wb') as stream:
            stream.write(b'original')
        self.git('add', os.fsdecode(invalid))
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'legacy filename')
        self.git('mv', os.fsdecode(invalid), 'valid-name')
        result = self.run_cmd([sys.executable, 'tools/intentctl.py', 'audit', '--json'], ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b'Traceback', result.stderr)
        self.assertIn('non-UTF-8 filenames', str(json.loads(result.stdout)))

    def test_installed_init_upgrades_legacy_hooks_and_preserves_originals(self):
        with tempfile.TemporaryDirectory(prefix='installed hooks ') as temp:
            prefix = Path(temp) / 'prefix with spaces'
            self.run_cmd([sys.executable, str(PROJECT / 'scripts/install.py'), '--prefix', str(prefix)])
            cli = str(prefix / 'bin/intentctl')
            hooks = self.root / '.git/hooks'
            originals = {'pre-commit': b'#!/bin/sh\necho original > .git/original-commit-ran\n',
                         'pre-push': b'#!/bin/sh\ncat > .git/original-push-input\n'}
            for name, original in originals.items():
                backup = hooks / (name + '.intentctl-original')
                backup.write_bytes(original)
                backup.chmod(0o755)
                legacy = '#!/bin/sh\n# intentctl managed dispatcher v1\nroot=$(git rev-parse --show-toplevel)\n'
                legacy += 'python3 "$root/tools/intentctl.py" validate ' + ('--staged\n' if name == 'pre-commit' else '--push\n')
                (hooks / name).write_text(legacy)
                (hooks / name).chmod(0o755)
            self.run_cmd([cli, 'init', '--no-instructions'])
            upgraded = {name: (hooks / name).read_bytes() for name in originals}
            for name in originals:
                self.assertIn(str(prefix / 'lib/intent-driven-engineering/intentctl.py').encode(), upgraded[name])
                self.assertNotIn(b'$root/tools/intentctl.py', upgraded[name])
                self.assertEqual((hooks / (name + '.intentctl-original')).read_bytes(), originals[name])
            self.run_cmd([cli, 'init', '--no-instructions'])
            self.assertEqual(upgraded, {name: (hooks / name).read_bytes() for name in originals})
            # Staged validation can run without any copied working-tree tools.
            shutil.move(self.root / 'tools', Path(temp) / 'old copied tools')
            self.git('commit', '--allow-empty', '-qm', 'upgraded hooks')
            self.assertTrue((self.root / '.git/original-commit-ran').exists())
            tip = self.git('rev-parse', 'HEAD').stdout.decode().strip()
            stdin = f'refs/heads/main {tip} refs/heads/main {tip}\n'.encode()
            self.run_cmd([str(hooks / 'pre-push'), 'origin', 'unused'], input=stdin)
            self.assertEqual((self.root / '.git/original-push-input').read_bytes(), stdin)
            self.run_cmd([cli, 'uninstall-hooks'])
            for name, original in originals.items():
                self.assertEqual((hooks / name).read_bytes(), original)
