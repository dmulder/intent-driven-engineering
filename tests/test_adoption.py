"""User-visible adoption workflows against real repositories and an installed CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
        self.assertTrue((self.root / 'AGENTS.md').read_bytes().startswith(original))
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
        self.assertEqual(result['audit'], once)
        self.assertIn('positive finite', str(self.ctl('watch', '--once', '--interval', 'nan', ok=False)))

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
            run(cli, 'uninstall-hooks')
            run(sys.executable, PROJECT / 'scripts/install.py', '--prefix', prefix, '--uninstall')
            self.assertFalse(cli.exists())
            self.assertTrue((repo / '.intent/config.json').exists())

    def test_installer_preserves_unmanaged_command(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix = Path(temp)
            (prefix / 'bin').mkdir()
            command = prefix / 'bin/intentctl'
            command.write_text('existing command\n')
            result = subprocess.run([sys.executable, str(PROJECT / 'scripts/install.py'), '--prefix', str(prefix)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(command.read_text(), 'existing command\n')
