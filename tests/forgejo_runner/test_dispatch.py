"""Runner experiments fail closed before accessing a runtime."""

from pathlib import Path
import os
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/forgejo_runner/test.py"


class DispatchTests(unittest.TestCase):
    def test_public_egress_requires_explicit_disposable_target(self):
        result = self.run_cli('host-fixture', '--egress-probe-only')
        self.assertEqual(1, result.returncode)
        self.assertIn('disposable', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def run_cli(self, *arguments):
        environment = os.environ.copy()
        environment["PATH"] = str(ROOT / ".tmp/nonexistent-runner-tools")
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=ROOT, env=environment, capture_output=True, text=True,
            timeout=15, check=False,
        )

    def test_invalid_mode_is_a_usage_error(self):
        result = self.run_cli("production")
        self.assertEqual(2, result.returncode)
        self.assertIn("usage:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_compatibility_without_podman_fails_without_traceback(self):
        result = self.run_cli("compatibility")
        self.assertEqual(1, result.returncode)
        self.assertIn("Podman", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_workflow_contracts_are_dispatched_to_the_bounded_runtime(self):
        result = self.run_cli('compatibility', '--workflow-contracts')
        self.assertEqual(1, result.returncode)
        self.assertIn('Podman', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_host_options_are_rejected_outside_host_mode(self):
        result = self.run_cli("compatibility", "--host-fixture", "target.json")
        self.assertEqual(2, result.returncode)
        self.assertNotIn("Traceback", result.stderr)

    def test_resource_probe_cannot_be_combined_with_other_host_modes(self):
        for arguments in [('unit', '--resource-probe-only'),
                          ('host-fixture', '--preflight-only', '--resource-probe-only'),
                          ('host-fixture', '--fixture', 'images.json', '--resource-probe-only')]:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(2, result.returncode)
                self.assertNotIn('Traceback', result.stderr)

    def test_host_preflight_requires_a_target_without_using_local_podman(self):
        result = self.run_cli("host-fixture", "--preflight-only")
        self.assertEqual(1, result.returncode)
        self.assertIn("disposable", result.stderr)
        self.assertNotIn("Podman", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_worker_probe_requires_explicit_target(self):
        result = self.run_cli('host-fixture', '--worker-probe-only')
        self.assertEqual(1, result.returncode)
        self.assertIn('disposable', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_worker_probe_cannot_mix_experiments(self):
        for arguments in [('unit', '--worker-probe-only'),
                          ('host-fixture', '--worker-probe-only', '--resource-probe-only'),
                          ('host-fixture', '--worker-probe-only', '--preflight-only'),
                          ('host-fixture', '--worker-probe-only', '--fixture', 'images.json')]:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(2, result.returncode)
                self.assertNotIn('Traceback', result.stderr)

    def test_native_job_probe_requires_both_private_descriptors(self):
        result = self.run_cli('host-fixture', '--job-probe-only')
        self.assertEqual(1, result.returncode)
        self.assertIn('disposable', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_network_probe_requires_explicit_target(self):
        result = self.run_cli('host-fixture', '--network-probe-only')
        self.assertEqual(1, result.returncode)
        self.assertIn('disposable', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_network_probe_rejects_other_experiments(self):
        for args in [('unit', '--network-probe-only'),
                     ('host-fixture', '--network-probe-only', '--preflight-only'),
                     ('host-fixture', '--network-probe-only', '--worker-probe-only'),
                     ('host-fixture', '--network-probe-only', '--resource-probe-only'),
                     ('host-fixture', '--network-probe-only', '--job-probe-only'),
                     ('host-fixture', '--network-probe-only', '--fixture', 'images.json')]:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(2, result.returncode)
                self.assertNotIn('Traceback', result.stderr)

    def test_native_job_probe_cannot_mix_experiments(self):
        for args in [('compatibility', '--job-probe-only'),
                     ('host-fixture', '--job-probe-only', '--worker-probe-only'),
                     ('host-fixture', '--job-probe-only', '--preflight-only'),
                     ('host-fixture', '--job-probe-only', '--resource-probe-only')]:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(2, result.returncode)
                self.assertNotIn('Traceback', result.stderr)

    def test_host_fixture_requires_an_explicit_disposable_target(self):
        result = self.run_cli("host-fixture")
        self.assertEqual(1, result.returncode)
        self.assertIn("disposable", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
