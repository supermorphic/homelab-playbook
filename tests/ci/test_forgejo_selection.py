"""Forgejo evidence follows the same selected scenario in local and hosted CI."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
import classify
import molecule_plan
import run_changed


class ForgejoSelectionTests(unittest.TestCase):
    def result(self, path):
        result = classify.classify_paths([path])
        result['molecule_plan'] = molecule_plan.build_plan(result)
        return result

    def selectors(self, result):
        return {row['selector'] for row in result['molecule_plan']['matrix']['include']}

    def test_each_forgejo_surface_selects_only_forgejo(self):
        for path in ('roles/forgejo/files/backup.py', 'playbooks/forgejo/verify.yml',
                     'scripts/forgejo/restore.py', 'tests/forgejo/test_restore.py'):
            with self.subTest(path=path):
                result = self.result(path)
                self.assertEqual('molecule', result['depth'])
                self.assertEqual({'forgejo/default'}, self.selectors(result))

    def test_unrelated_and_docs_changes_exclude_forgejo(self):
        for path in ('roles/system_maintenance/tasks/main.yml', 'docs/guides/forgejo-recovery.md',
                     'roles/forgejo/README.md', 'playbooks/forgejo/README.md'):
            with self.subTest(path=path):
                self.assertNotIn('forgejo/default', self.selectors(self.result(path)))

    def test_foundation_and_proxy_dependencies_include_forgejo(self):
        for path in ('roles/podman_foundation/tasks/main.yml', 'roles/reverse_proxy/tasks/main.yml',
                     'roles/tls_automation/tasks/main.yml'):
            with self.subTest(path=path):
                self.assertIn('forgejo/default', self.selectors(self.result(path)))
                self.assertGreater(len(self.selectors(self.result(path))), 1)

    def test_full_fallback_includes_forgejo_and_runtime_modes(self):
        for path in ('unknown/file', 'uv.lock', 'scripts/molecule.py',
                     'roles/forgejo/molecule/default/create.yml'):
            with self.subTest(path=path):
                result = self.result(path)
                self.assertEqual('full', result['depth'])
                self.assertIn('forgejo/default', self.selectors(result))
        commands = run_changed.commands_for(self.result('roles/forgejo/tasks/main.yml'))
        for mode in ('compatibility', 'fixture'):
            self.assertEqual(1, commands.count(['mise', 'run', 'test:forgejo', '--', mode]))
        self.assertFalse(any('test:forgejo' in cmd for cmd in run_changed.commands_for(
            self.result('roles/system_maintenance/tasks/main.yml'))))

    def test_hosted_runtime_commands_are_conditioned_on_selected_row(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn("matrix.selector == 'forgejo/default' && matrix.platform == 'debian13'", workflow)
        self.assertIn('mise run test:forgejo -- compatibility', workflow)
        self.assertIn('mise run test:forgejo -- fixture', workflow)


if __name__ == '__main__':
    unittest.main()
