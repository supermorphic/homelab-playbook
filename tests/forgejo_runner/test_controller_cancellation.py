"""A revoked ephemeral registration must release its owned controller promptly."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

FILES = Path(__file__).parents[2] / 'roles/forgejo_runner/files'
sys.path.insert(0, str(FILES))
spec = importlib.util.spec_from_file_location('cancellation_resources', FILES / 'resources.py')
resources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resources)
from registration import RegistrationUncertain


class ControllerCancellationTests(unittest.TestCase):
    def child(self, seconds):
        child = subprocess.Popen(
            [sys.executable, '-c',
             f'import sys,time; sys.stdin.buffer.read(); time.sleep({seconds})'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.dispose, child)
        return child

    @staticmethod
    def dispose(child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        if child.stdin:
            child.stdin.close()

    def wait(self, child, present, seconds=5):
        return resources.wait_controller(child, b'{}', job_seconds=seconds,
                                         registration_present=present, poll_seconds=0.05)

    def test_removed_registration_terminates_a_real_controller(self):
        child = self.child(60)
        with self.assertRaises(RuntimeError):
            self.wait(child, lambda: False)
        self.assertIsNotNone(child.poll())
        self.assertNotEqual(0, child.returncode)

    def test_present_registration_allows_normal_completion(self):
        child = self.child(0.2)
        self.wait(child, lambda: True)
        self.assertEqual(0, child.returncode)

    def test_api_failure_does_not_mean_registration_was_removed(self):
        child = self.child(0.2)
        def uncertain():
            raise RegistrationUncertain('Synthetic API failure')
        self.wait(child, uncertain)
        self.assertEqual(0, child.returncode)

    def test_unreachable_api_does_not_extend_the_job_deadline(self):
        child = self.child(60)
        def uncertain():
            raise RegistrationUncertain('Synthetic API failure')
        with self.assertRaises(subprocess.TimeoutExpired):
            self.wait(child, uncertain, seconds=0.2)
        self.assertIsNotNone(child.poll())

    def test_unexpected_observation_failure_still_terminates_controller(self):
        child = self.child(60)
        def invalid():
            raise ValueError('Synthetic identity failure')
        with self.assertRaises(ValueError):
            self.wait(child, invalid)
        self.assertIsNotNone(child.poll())


if __name__ == '__main__':
    unittest.main()
