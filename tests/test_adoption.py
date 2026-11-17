"""User-visible adoption workflows against real repositories and an installed CLI."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from support import PROJECT, RepoTest


class AdoptionTests(RepoTest):
    def test_deploy_preserves_agents_and_refuses_conflicting_protocol(self):
        self.write('AGENTS.md', '# Local policy\nDo not edit generated files.\n')
        self.ctl('deploy')
        self.ctl('deploy')
        text = (self.root / 'AGENTS.md').read_text()
        self.assertTrue(text.startswith('# Local policy\nDo not edit generated files.\n'))
        self.assertEqual(text.count('<!-- intentctl:begin -->'), 1)
        (self.root / 'INTENT.md').write_text('# Independently maintained protocol\n')
        original = (self.root / 'AGENTS.md').read_bytes()
        result = self.ctl('deploy', '--update', ok=False)
        self.assertIn('already exists', str(result))
        self.assertEqual((self.root / 'AGENTS.md').read_bytes(), original)
        self.assertEqual((self.root / 'INTENT.md').read_text(), '# Independently maintained protocol\n')

    def test_deploy_preserves_crlf_and_updates_only_managed_protocol(self):
        original = b'# Local instructions\r\nKeep this exact text.\r\n'
        (self.root / 'AGENTS.md').write_bytes(original)
        self.ctl('deploy')
        deployed = (self.root / 'AGENTS.md').read_bytes()
        self.assertTrue(deployed.startswith(original))
        self.assertNotIn(b'\n', deployed.replace(b'\r\n', b''))
        self.ctl('deploy')
        self.assertEqual((self.root / 'AGENTS.md').read_bytes(), deployed)
        self.write('INTENT.md', '<!-- intentctl:begin -->\nOld managed protocol\n<!-- intentctl:end -->\n')
        self.ctl('deploy', '--update')
        self.assertIn('Adopting existing repositories', (self.root / 'INTENT.md').read_text())

    def test_audit_detects_removed_system_and_rejects_shallow_history(self):
        base = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.git('rm', '-rf', '.intent')
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Remove metadata without accounting')
        result = self.ctl('audit', '--since', base, ok=False)
        self.assertIn('REMOVED_SYSTEM', str(result))
        self.write('.git/shallow', self.git('rev-parse', 'HEAD').stdout.decode())
        self.assertIn('full history', str(self.ctl('audit', ok=False)))

    def test_non_utf8_instruction_files_fail_without_writes(self):
        originals = {name: (self.root / name).read_bytes() for name in ('AGENTS.md', 'INTENT.md')}
        for name in originals:
            for command in ('deploy', 'init'):
                with self.subTest(name=name, command=command):
                    for path, data in originals.items():
                        self.write(path, data)
                    self.write(name, b'# Notes \xe9\n')
                    before = {str(p): p.read_bytes() for p in (self.root / '.intent').rglob('*.json')}
                    result = self.run_cmd([sys.executable, 'tools/intentctl.py', command, '--json'], ok=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(b'Traceback', result.stderr)
                    self.assertIn(f'{name} is not valid UTF-8', json.loads(result.stdout)['errors'][0]['message'])
                    self.assertEqual((self.root / name).read_bytes(), b'# Notes \xe9\n')
                    self.assertEqual(before, {str(p): p.read_bytes() for p in (self.root / '.intent').rglob('*.json')})

    def test_since_non_ancestor_has_actionable_error(self):
        self.git('checkout', '-qb', 'other')
        self.git('commit', '--allow-empty', '-qm', 'other branch')
        other = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.git('checkout', '-q', '-')
        result = self.ctl('audit', '--since', other, ok=False)
        self.assertIn(f'--since {other} is not an ancestor of HEAD', str(result))

    def test_audit_caches_immutable_commits_and_exact_working_state(self):
        from intentlib.adoption import audit
        from intentlib.cli import run_validation, validate_commit
        cache = {}
        with patch('intentlib.cli.validate_commit', wraps=validate_commit) as commits, \
             patch('intentlib.cli.run_validation', wraps=run_validation) as working:
            first = audit(self.root, cache=cache)
            self.assertFalse(first['errors'])
            counts = commits.call_count, working.call_count
            self.assertEqual(audit(self.root, cache=cache), first)
            self.assertEqual((commits.call_count, working.call_count), counts)
            self.write('auth.py', 'changed without intent\n')
            self.git('add', 'auth.py')
            self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'missing event')
            self.assertTrue(audit(self.root, cache=cache)['errors'])
            self.assertEqual((commits.call_count, working.call_count), (counts[0] + 1, counts[1] + 1))
            self.write('auth.py', 'another uncommitted change\n')
            changed = audit(self.root, cache=cache)
            self.assertNotEqual(changed['state_fingerprint'], first['state_fingerprint'])
            self.assertEqual((commits.call_count, working.call_count), (counts[0] + 1, counts[1] + 2))

    def test_historical_schema_drift_is_separate_but_current_pins_are_strict(self):
        schema = '.intent/schema/binding.schema.json'
        current = (self.root / schema).read_bytes()
        base = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.write(schema, '{}\n')
        self.git('add', schema)
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'legacy schema pins')
        old = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.write(schema, current)
        self.git('add', schema)
        self.commit('current schema pins')
        audit = self.ctl('audit', '--since', base, ok=False)
        self.assertFalse(audit['errors'])
        self.assertEqual({d['commit'] for d in audit['historical_schema_drift']}, {old})
        self.assertIn('SCHEMA_DEFINITION', str(self.ctl('validate', '--commit', old, ok=False)))
        self.write(schema, '{}\n')
        self.assertTrue(any(e['code'] == 'SCHEMA_DEFINITION' for e in self.ctl('audit', '--since', base, ok=False)['errors']))

    def test_historical_schema_drift_cannot_hide_unaccounted_changes(self):
        schema = '.intent/schema/binding.schema.json'
        current = (self.root / schema).read_bytes()
        base = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.write(schema, '{}\n')
        self.write('auth.py', 'missing source event\n')
        self.git('add', schema, 'auth.py')
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'old schema and missing accounting')
        bad = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.write(schema, current)
        self.git('add', schema)
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'update schemas only')
        result = self.ctl('audit', '--since', base, ok=False)
        self.assertTrue(any(e['commit'] == bad and e['code'] == 'UNCOVERED_CHANGE' for e in result['errors']))
        self.assertTrue(result['historical_schema_drift'])

    def test_watch_reports_transient_errors_and_recovers(self):
        from intentlib.cli import main
        from intentlib.core import IntentError
        stream = io.StringIO()
        with patch('intentlib.cli.root_path', return_value=self.root), \
             patch('intentlib.adoption.audit', side_effect=[IntentError('concurrent write'), {'needs_attention': False}]), \
             patch('intentlib.cli.time.sleep', side_effect=[None, KeyboardInterrupt]), \
             contextlib.redirect_stdout(stream):
            self.assertEqual(main(['watch', '--json']), 0)
        states = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len(states), 2)
        self.assertTrue(states[0]['needs_attention'])
        self.assertIn('concurrent write', str(states[0]))
        self.assertFalse(states[1]['needs_attention'])

    def test_symlink_instructions_rejected(self):
        (self.root / 'INTENT.md').unlink()
        os.symlink('auth.py', self.root / 'INTENT.md')
        before = (self.root / 'auth.py').read_bytes()
        self.assertIn('symlink', str(self.ctl('deploy', ok=False)))
        self.assertEqual((self.root / 'auth.py').read_bytes(), before)

    def test_audit_finds_committed_human_drift_and_is_read_only(self):
        # Include the initially deployed instructions in an explicitly accounted event.
        self.git('add', 'AGENTS.md', 'INTENT.md')
        self.capture('instructions')
        self.finish('instructions', cover=True)
        clean = self.ctl('audit')
        self.assertFalse(clean['needs_attention'])
        self.assertTrue(clean['adoption_backlog'])
        checkpoint = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.write('auth.py', 'def authorized(token):\n    return True\n')
        self.git('add', 'auth.py')
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'human edit without intent')
        offending = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.write('untracked private.txt', 'must not appear in review packet')
        index_before = (self.root / '.git/index').read_bytes()
        metadata_before = {str(p): p.read_bytes() for p in (self.root / '.intent').rglob('*.json')}
        result = self.ctl('reconcile', '--since', checkpoint, ok=False)
        self.assertTrue(result['audit']['needs_attention'])
        self.assertTrue(any(e.get('commit') == offending for e in result['audit']['errors']))
        self.assertIn('untracked private.txt', str(result))
        self.assertNotIn('must not appear', str(result))
        self.assertEqual(index_before, (self.root / '.git/index').read_bytes())
        self.assertEqual(metadata_before, {str(p): p.read_bytes() for p in (self.root / '.intent').rglob('*.json')})
        once = self.ctl('watch', '--once', '--since', checkpoint, ok=False)
        self.assertEqual({'output_version': 1, **result['audit']}, once)
        self.assertIn('positive finite', str(self.ctl('watch', '--once', '--interval', 'nan', ok=False)))

    def test_report_resolves_live_coordinates_without_rewriting_pins(self):
        original = (self.root / 'auth.py').read_bytes()
        start = original.index(b'return')
        end = start + len(b'return')
        _, binding_id = self.bound('return-location', start=start, end=end)
        pinned = self.ctl('show-binding', binding_id)['target']
        prefix = b'# Context added before the implementation.\n'
        self.write('auth.py', prefix + original)
        self.git('add', 'auth.py')
        self.capture('add-context')
        self.finish('add-context', cover=True)
        packet = self.ctl('report')
        binding = next(b for b in packet['bindings'] if b['id'] == binding_id)
        self.assertEqual(binding['target'], pinned)
        self.assertEqual(binding['live_target'], {
            'path': 'auth.py', 'start': start + len(prefix),
            'end': end + len(prefix), 'mode': '100644'})
        markdown = self.run_cmd([sys.executable, 'tools/intentctl.py', 'report']).stdout.decode()
        self.assertIn(f"current: auth.py bytes {start + len(prefix)}..{end + len(prefix)}", markdown)
        self.assertIn(f"pinned snapshot {pinned['source']}: auth.py bytes {start}..{end}", markdown)

    def test_report_labels_unavailable_and_historical_locations(self):
        intent_id, binding_id = self.bound('location-status', start=0, end=8)
        pinned = self.ctl('show-binding', binding_id)['target']
        self.write('auth.py', 'Entirely different implementation\n')
        binding = next(b for b in self.ctl('report')['bindings'] if b['id'] == binding_id)
        self.assertIsNone(binding['live_target'])
        markdown = self.run_cmd([sys.executable, 'tools/intentctl.py', 'report']).stdout.decode()
        self.assertIn('current location unavailable', markdown)
        self.assertIn(f"pinned snapshot {pinned['source']}: auth.py bytes 0..8", markdown)
        self.git('restore', 'auth.py')
        self.capture('end-applicability')
        self.preserve('end-applicability', intent_id, binding_id, action='unapply', status='ended', start=0, end=8)
        self.finish('end-applicability')
        binding = next(b for b in self.ctl('report')['bindings'] if b['id'] == binding_id)
        self.assertIsNone(binding['live_target'])
        markdown = self.run_cmd([sys.executable, 'tools/intentctl.py', 'report']).stdout.decode()
        self.assertIn(f"historical pinned snapshot {pinned['source']}: auth.py bytes 0..8", markdown)
        self.assertNotIn('current: auth.py bytes 0..8', markdown)

    def test_report_shows_rationale_unknown_regions_and_reassessment(self):
        i, b = self.bound('security-policy', start=0, end=8)
        packet = self.ctl('report')
        self.assertIn('auth.py', [x['path'] for x in packet['adoption_backlog']])
        self.capture('revised-policy')
        self.ctl('intent-revise', i, '--event', 'revised-policy', '--requirement', 'New policy')
        self.ctl('declare', '--event', 'revised-policy', '--intent', i, '--action', 'modify')
        self.finish('revised-policy')
        packet = self.ctl('report')
        self.assertIn(b, packet['reassessment'])
        markdown = self.run_cmd([sys.executable, 'tools/intentctl.py', 'report']).stdout.decode()
        self.assertIn('Explicit rationale', markdown)
        self.assertIn('Unknown provenance', markdown)
        self.assertIn('New policy', markdown)
        self.assertIn(b, markdown)


class InstalledTests(unittest.TestCase):
    def test_installed_command_in_foreign_repo_and_hooks_without_tools(self):
        with tempfile.TemporaryDirectory(prefix='intent install ') as temp:
            root = Path(temp)
            prefix, repo = root / 'user prefix', root / 'foreign repo'
            repo.mkdir()
            env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                   'PYTHONDONTWRITEBYTECODE': '1'}
            def run(*args, ok=True, cwd=repo):
                result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, capture_output=True)
                if ok:
                    self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
                return result
            run(sys.executable, PROJECT / 'scripts/install.py', '--prefix', prefix)
            run(sys.executable, PROJECT / 'scripts/install.py', '--prefix', prefix)  # managed upgrade
            cli = prefix / 'bin/intentctl'
            run('git', 'init', '-q')
            run('git', 'config', 'user.name', 'Tests')
            run('git', 'config', 'user.email', 'tests@example.invalid')
            run('git', 'config', 'commit.gpgsign', 'false')
            initial = run(cli, 'audit', '--json', ok=False)
            self.assertFalse(json.loads(initial.stdout)['initialized'])
            # Conflicts fail before metadata or hooks are installed.
            (repo / 'INTENT.md').write_text('Existing human notes\n')
            self.assertNotEqual(run(cli, 'init', ok=False).returncode, 0)
            self.assertFalse((repo / '.intent').exists())
            (repo / 'INTENT.md').unlink()
            (repo / 'legacy.txt').write_text('legacy behavior\n')
            run('git', 'add', 'legacy.txt')
            run(cli, 'init')
            run('git', 'add', 'AGENTS.md', 'INTENT.md')
            run(cli, 'baseline')
            run('git', 'add', '.intent')
            run('git', 'commit', '-qm', 'Unknown provenance import')
            self.assertFalse((repo / 'tools').exists())
            nested = repo / 'nested'
            nested.mkdir()
            self.assertEqual(json.loads(run(cli, 'status', '--json', cwd=nested).stdout)['intents'], 0)
            self.assertFalse(json.loads(run(cli, 'audit', '--json').stdout)['needs_attention'])
            (repo / 'legacy.txt').write_text('unaccounted new behavior\n')
            run('git', 'add', 'legacy.txt')
            self.assertNotEqual(run('git', 'commit', '-qm', 'Missing event', ok=False).returncode, 0)
            engine = prefix / 'lib/intent-driven-engineering/intentctl.py'
            engine.rename(engine.with_suffix('.missing'))
            missing = run('git', 'commit', '-qm', 'engine moved', ok=False)
            self.assertIn(b'intentctl is unavailable', missing.stderr)
            self.assertIn(b'reinstall at the same prefix', missing.stderr)
            engine.with_suffix('.missing').rename(engine)
            removed = run(sys.executable, PROJECT / 'scripts/install.py', '--prefix', prefix, '--uninstall')
            self.assertIn(b'Warning: hooks may still reference', removed.stderr)
            missing = run('git', 'commit', '-qm', 'engine removed', ok=False)
            self.assertIn(b'intentctl is unavailable', missing.stderr)
            run(sys.executable, PROJECT / 'scripts/install.py', '--prefix', prefix)
            run(cli, 'uninstall-hooks')
            run(sys.executable, PROJECT / 'scripts/install.py', '--prefix', prefix, '--uninstall')
            self.assertFalse(cli.exists())
            self.assertTrue((repo / '.intent/config.json').exists())

    def test_launcher_with_spaced_and_quoted_interpreter_path(self):
        with tempfile.TemporaryDirectory(prefix='intent launcher ') as temp:
            root = Path(temp)
            environment = root / "python env 'quoted'"
            prefix = root / "install prefix 'quoted'"
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(environment)],
                           check=True, env=env, capture_output=True)
            python = environment / 'bin/python'
            installed = subprocess.run([str(python), str(PROJECT / 'scripts/install.py'),
                                        '--prefix', str(prefix)], env=env, capture_output=True)
            self.assertEqual(installed.returncode, 0, installed.stderr.decode())
            # Run the installed executable directly so the OS parses its shebang.
            result = subprocess.run([str(prefix / 'bin/intentctl'), 'report', '--help'],
                                    env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertIn(b'--json', result.stdout)
            repo = root / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=env, capture_output=True)
            subprocess.run([str(prefix / 'bin/intentctl'), 'init'], cwd=repo, check=True, env=env, capture_output=True)
            environment.rename(root / 'interpreter removed')
            hook = subprocess.run([str(repo / '.git/hooks/pre-commit')], cwd=repo, env=env, capture_output=True)
            self.assertEqual(hook.returncode, 1)
            self.assertIn(b'intentctl is unavailable', hook.stderr)

    def test_installer_preserves_unmanaged_command(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix = Path(temp)
            (prefix / 'bin').mkdir()
            command = prefix / 'bin/intentctl'
            for data in (b'existing command\n', b'\x7fELF\xff\x00'):
                command.write_bytes(data)
                result = subprocess.run([sys.executable, str(PROJECT / 'scripts/install.py'), '--prefix', str(prefix)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'refusing to replace unmanaged command', result.stderr)
                self.assertNotIn(b'Traceback', result.stderr)
                self.assertEqual(command.read_bytes(), data)

    def test_packaged_protocol_matches_repository_protocol(self):
        protocol = (PROJECT / 'tools/intentlib/protocol.md').read_bytes()
        self.assertEqual(protocol, (PROJECT / 'INTENT.md').read_bytes())
        self.assertNotIn(b'unittest discover', protocol)
        self.assertIn(b"repository's own test suite", protocol)

    def test_failed_launcher_publication_cleans_temporary_file(self):
        spec = importlib.util.spec_from_file_location('intent_installer', PROJECT / 'scripts/install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(sys, 'argv', ['install.py', '--prefix', temp]), \
             patch.object(installer.os, 'replace', side_effect=OSError('publication failed')), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(installer.main(), 1)
            self.assertFalse(list((Path(temp) / 'bin').glob('.intentctl-*')))
