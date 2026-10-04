"""Workload probes must run the selected repository checks and bind evidence to a commit."""

from pathlib import Path
import importlib
import subprocess
import tempfile
import unittest


class WorkloadTests(unittest.TestCase):
    def module(self):
        path = Path(__file__).resolve().parents[2] / 'scripts/forgejo_runner/workload.py'
        self.assertTrue(path.is_file(), 'Real Forgejo workload probe is missing')
        return importlib.import_module('scripts.forgejo_runner.workload')

    def test_failed_repository_command_fails_the_job(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'mise').write_text('#!/bin/sh\nexit 19\n')
            (root / 'mise').chmod(0o755)
            result = subprocess.run(['bash', '-ec', module.validation_script('forgejo/default')],
                                    env={'PATH': directory + ':/usr/bin:/bin'},
                                    cwd=root, capture_output=True, text=True)
            self.assertEqual(19, result.returncode)

    def test_selected_repository_workload_runs_after_bootstrap(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'mise').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> calls\n')
            (root / 'mise').chmod(0o755)
            result = subprocess.run(['bash', '-ec', module.validation_script('forgejo/default')],
                                    env={'PATH': directory + ':/usr/bin:/bin'},
                                    cwd=root, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(['trust --yes', 'install --locked', 'run bootstrap',
                              'run test:molecule -- forgejo/default'],
                             (root / 'calls').read_text().splitlines())

    def test_unknown_workload_cannot_become_a_shell_command(self):
        module = self.module()
        with self.assertRaises(ValueError):
            module.validation_script('forgejo/default; echo unexpected')

    def test_source_snapshot_excludes_untracked_operator_files(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(['git', '-C', directory, *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            (root / 'candidate.txt').write_text('tracked candidate\n')
            (root / 'operator-secret.txt').write_text('must not enter job\n')
            git('add', 'candidate.txt')
            tree = module.source_tree(root)
            self.assertEqual('candidate.txt', git('ls-tree', '--name-only', tree))
            (root / 'candidate.txt').write_text('unstaged change\n')
            with self.assertRaises(RuntimeError):
                module.source_tree(root)

    def test_workload_uses_the_repository_molecule_lock(self):
        module = self.module()
        self.assertTrue(hasattr(module, 'workload_lock'), 'Workload lacks the host invocation lock')
        from scripts.molecule import invocation_lock, SubprocessCommandRunner, PreflightError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', directory], check=True)
            with invocation_lock(root, SubprocessCommandRunner()):
                with self.assertRaises(PreflightError):
                    with module.workload_lock(root):
                        self.fail('Concurrent workload acquired the repository lock')

    def test_success_must_match_the_workflow_commit(self):
        module = self.module()
        with self.assertRaises(RuntimeError):
            module.require_success([{'status': 'success', 'head_sha': 'a' * 40}], 'b' * 40)
        with self.assertRaises(RuntimeError):
            module.require_success([{'status': 'failure', 'head_sha': 'b' * 40}], 'b' * 40)
        module.require_success([{'status': 'success', 'head_sha': 'b' * 40}], 'b' * 40)


if __name__ == '__main__':
    unittest.main()
