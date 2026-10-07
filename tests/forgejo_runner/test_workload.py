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

    def test_stock_runtime_suite_prepares_the_image_then_runs_the_five_pinned_tasks(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'mise').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> calls\n')
            (root / 'mise').chmod(0o755)
            result = subprocess.run(['bash', '-ec', module.validation_script(suite='stock-runtime')],
                                    env={'PATH': directory + ':/usr/bin:/bin'}, cwd=root,
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(['trust --yes', 'install --locked', 'run bootstrap',
                'run test:molecule -- semaphore/default',
                'run test:forgejo -- compatibility', 'run test:forgejo -- fixture',
                'run test:semaphore -- compatibility', 'run test:semaphore -- fixture',
                'run test:semaphore -- controller'], (root / 'calls').read_text().splitlines())
        for kwargs in ({'suite':'stock-runtime; false'}, {'selector':'forgejo/default', 'suite':'stock-runtime'}, {}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                module.validation_script(**kwargs)

    def execute(self, *, prerequisite_fails=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mise = root / 'mise'
            mise.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> calls
case "$*" in
  "run test:molecule -- semaphore/default")
    if [ "$FAIL_PREREQUISITE" = 1 ]; then exit 23; fi
    touch semaphore-image ;;
  "run test:semaphore -- controller")
    if [ ! -f semaphore-image ]; then exit 24; fi ;;
esac
''')
            mise.chmod(0o755)
            env = {'PATH': directory + ':/usr/bin:/bin',
                   'FAIL_PREREQUISITE': str(int(prerequisite_fails))}
            result = subprocess.run(['bash', '-ec', self.module().validation_script(suite='stock-runtime')],
                                    cwd=root, env=env, capture_output=True, text=True)
            calls = (root / 'calls').read_text().splitlines()
            return result, calls

    def test_stock_controller_requires_same_job_image_preparation(self):
        result, calls = self.execute()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, calls.count('run test:molecule -- semaphore/default'))
        self.assertLess(calls.index('run test:molecule -- semaphore/default'),
                        calls.index('run test:semaphore -- controller'))
        self.assertEqual(5, sum(call.startswith(('run test:forgejo --', 'run test:semaphore --'))
                                for call in calls))

    def test_failed_image_preparation_stops_stock_commands(self):
        result, calls = self.execute(prerequisite_fails=True)
        self.assertEqual(23, result.returncode)
        self.assertFalse(any(call.startswith(('run test:forgejo --', 'run test:semaphore --'))
                             for call in calls))

    def test_stock_policy_admits_only_canonical_prerequisite_base(self):
        sources = self.module().registry_sources(suite='stock-runtime')
        self.assertIn('docker.io/library/debian:13', sources)
        self.assertNotIn('docker.io/library/debian', sources)


    def test_native_workflow_clones_the_private_loopback_application(self):
        import yaml
        from types import SimpleNamespace
        application = SimpleNamespace(app='container-name', user='fixture', url='http://127.0.0.1:12345')
        document = yaml.safe_load(self.module().workflow('fixture-label', application,
            {'name':'repository'}, Path('/work/job-workspace'), 'forgejo/default',
            clone_base=application.url, public_probe=True))
        script = document['jobs']['first']['steps'][0]['run']
        self.assertIn('git clone http://127.0.0.1:12345/fixture/repository.git ', script)
        self.assertIn('python3 "$TMPDIR/public-probe.py"', script)
        self.assertEqual('unexpected-second-job', document['jobs']['second']['steps'][0]['run'].split()[-1])

    def test_archived_source_must_reconstruct_the_exact_git_tree(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); original=root/'original'; restored=root/'restored'
            original.mkdir(); restored.mkdir()
            def git(path,*args):
                return subprocess.check_output(['git','-C',str(path),*args],text=True).strip()
            git(original,'init','-q')
            (original/'executable').write_text('#!/bin/sh\necho candidate\n')
            (original/'executable').chmod(0o755)
            (original/'link').symlink_to('executable')
            git(original,'add','--all'); tree=git(original,'write-tree')
            archive=root/'source.tar'
            subprocess.run(['git','-C',str(original),'archive','--format=tar','--output',str(archive),tree],check=True)
            import tarfile
            with tarfile.open(archive) as t: t.extractall(restored,filter='data')
            self.assertEqual(tree,module.restore_source_tree(restored,tree))
            self.assertEqual(tree,git(restored,'write-tree'))
            changed=root/'changed'; changed.mkdir(); (changed/'different').write_text('different')
            with self.assertRaises(ValueError): module.restore_source_tree(changed,tree)

    def test_failed_public_probe_fails_the_real_job_script(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'python3'
            executable.write_text('#!/bin/sh\nif [ "$#" -eq 0 ]; then /bin/cat >/dev/null; exit 0; fi\nexit 19\n')
            executable.chmod(0o755)
            document = yaml.safe_load(self.module().runtime_workflow('synthetic', root, public_probe=True))
            script = document['jobs']['first']['steps'][0]['run']
            result = subprocess.run(['bash', '-ec', script], env={'PATH': directory + ':/usr/bin:/bin',
                'TMPDIR': directory}, cwd=root, capture_output=True, text=True)
            self.assertEqual(19, result.returncode)

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
