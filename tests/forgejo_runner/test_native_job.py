"""Native jobs remain a bounded, offline stage, separate from full acceptance."""

import importlib
import unittest
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from test_host_fixture import target_descriptor


class NativeJobTests(unittest.TestCase):
    def module(self):
        return importlib.import_module('scripts.forgejo_runner.native_job')

    def target(self):
        target = target_descriptor()
        target['limits'].update(memory_bytes=2 * 1024**3, disk_bytes=8 * 1024**3,
                                pids=512, cpu_percent=50, job_seconds=600)
        return target

    def test_job_budget_preserves_four_gib_of_service_memory_and_staging_disk(self):
        module = self.module(); target = self.target()
        capacity = {'memory_total': 7 * 1024**3, 'memory_available': 6 * 1024**3,
                    'disk_available': 20 * 1024**3}
        module.validate_job_budget(target, capacity)
        for change in ({'memory_available': 5 * 1024**3}, {'disk_available': 10 * 1024**3}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.validate_job_budget(target, {**capacity, **change})
        for key, value in (('memory_bytes', 4 * 1024**3), ('disk_bytes', 32 * 1024**3),
                           ('pids', 2048), ('cpu_percent', 100), ('job_seconds', 7200)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                module.validate_job_budget({**target, 'limits': {**target['limits'], key: value}})

    def test_job_observer_requires_all_workload_and_independent_boundary_outcomes(self):
        module = self.module()
        baseline = {k: True for k in ('rootless_api', 'sibling_containers', 'mapped_bind',
            'private_loopback', 'published_loopback', 'user_manager', 'bounded_storage')}
        with patch('scripts.forgejo_runner.host_worker.observe_worker_boundary',
                   return_value={**baseline, 'outer_limits': True, 'process_boundary': True}) as oracle:
            for observations in (baseline, {**baseline, 'one_job': False},
                                 {**baseline, 'one_job': True, 'job_runtime': False}):
                with self.subTest(observations=observations), self.assertRaises(ValueError):
                    module.observe_job_boundary(self.target(), observations, {})
            oracle.assert_not_called()
            result = module.observe_job_boundary(self.target(),
                {**baseline, 'one_job': True, 'job_runtime': True}, {})
            self.assertTrue(result['outer_limits'])
            self.assertTrue(result['process_boundary'])
            self.assertTrue(result['one_job'])
            oracle.assert_called_once()

    def test_native_runner_remaps_only_reviewed_image_pins_to_verified_local_images(self):
        module = self.module()
        pin = 'example.invalid/image@sha256:' + 'a' * 64
        tag = 'localhost/native-image:fixture'
        run = module.NativeRun({pin: tag}, '/run/user/2202/podman.sock')
        with patch('scripts.forgejo.runtime.Run.command', return_value=subprocess.CompletedProcess([], 0, '', '')) as command:
            run.command(['/usr/bin/podman', 'run', pin, 'echo', 'unchanged'])
            self.assertEqual(['/usr/bin/podman', '--remote', '--url=unix:/run/user/2202/podman.sock',
                              'run', tag, 'echo', 'unchanged'], command.call_args.args[0])

    def test_verified_oci_load_uses_its_exact_local_policy_path(self):
        module = self.module()
        run = module.NativeRun({}, '/run/user/2202/podman.sock')
        with patch('scripts.forgejo.runtime.Run.command', return_value=subprocess.CompletedProcess([], 0, '', '')) as command:
            run.command(['/usr/bin/podman', 'load', '-i', '/work/input/image-0.oci'])
            self.assertEqual(['/usr/bin/podman', 'load', '-i', '/work/input/image-0.oci'],
                             command.call_args.args[0])

    def test_runtime_job_failure_cannot_become_a_successful_workflow_step(self):
        import yaml
        from scripts.forgejo_runner.workload import runtime_workflow
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'podman').write_text('#!/bin/sh\necho synthetic-api-error >&2\nexit 19\n'); (root / 'podman').chmod(0o755)
            document = yaml.safe_load(runtime_workflow('synthetic-label', root))
            script = document['jobs']['first']['steps'][0]['run']
            import os
            environment = {**os.environ, 'PATH': directory + ':' + os.environ['PATH']}
            result = subprocess.run(['bash', '-ec', script], env=environment,
                                    capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertIn('synthetic-api-error', result.stderr)

    def test_failed_job_diagnostic_is_bounded_and_private(self):
        module = self.module()
        import base64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'synthetic123.runner.log').write_text('runner: task failed\n')
            (root / 'synthetic123.task-0.base64').write_bytes(base64.b64encode(b'Error: synthetic job failure\n'))
            output = root / 'native-job-error.log'
            module.save_job_diagnostic('synthetic123', root, output)
            self.assertIn('synthetic job failure', output.read_text())
            self.assertLessEqual(output.stat().st_size, 4096)
            self.assertEqual(0o600, output.stat().st_mode & 0o777)

    def test_job_diagnostic_does_not_follow_worker_controlled_symlinks(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / 'sentinel'; sentinel.write_text('unrelated protected sentinel')
            (root / 'synthetic123.runner.log').symlink_to(sentinel)
            output = root / 'native-job-error.log'
            module.save_job_diagnostic('synthetic123', root, output)
            self.assertNotIn('unrelated protected sentinel', output.read_text())
            self.assertEqual('unrelated protected sentinel', sentinel.read_text())

    def test_offline_load_failure_preserves_bounded_precredential_diagnostic(self):
        module = self.module()
        from unittest.mock import Mock
        experiment = Mock(podman='/usr/bin/podman')
        experiment.command.return_value = subprocess.CompletedProcess([], 125, '', 'synthetic policy rejection')
        with self.assertRaisesRegex(RuntimeError, 'synthetic policy rejection'):
            module.load_images(experiment, [{'source': 'reviewed', 'id': 'a' * 64, 'asset': 'image-0.oci'}])

    def test_backend_error_retains_private_stderr_without_console_or_stdout_credentials(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native-error.log'
            run = module.NativeRun({}, '/run/user/2202/podman.sock', error_log=path)
            failure = subprocess.CompletedProcess([], 127, 'sensitive synthetic stdout', 'missing synthetic binary')
            with patch('scripts.forgejo.runtime.Run.command', return_value=failure):
                with self.assertRaises(RuntimeError) as caught:
                    run.command(['/usr/bin/podman', 'run', 'localhost/image:fixture'])
            self.assertNotIn('missing synthetic binary', str(caught.exception))
            self.assertNotIn('sensitive synthetic stdout', path.read_text())
            self.assertEqual('missing synthetic binary', path.read_text())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_loaded_image_wrong_identity_or_architecture_is_rejected(self):
        module = self.module()
        from unittest.mock import Mock
        import json
        for document in ({'Id': 'b' * 64, 'Architecture': 'amd64'},
                         {'Id': 'a' * 64, 'Architecture': 'arm64'}):
            experiment = Mock(podman='/usr/bin/podman')
            experiment.command.side_effect = [subprocess.CompletedProcess([], 0, '', ''),
                subprocess.CompletedProcess([], 0, json.dumps([document]), '')]
            with self.subTest(document=document), self.assertRaises(ValueError):
                module.load_images(experiment, [{'source': 'reviewed', 'id': 'a' * 64, 'asset': 'image-0.oci'}])


if __name__ == '__main__':
    unittest.main()
