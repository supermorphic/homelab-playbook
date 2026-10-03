"""Runner experiments fail closed before accessing a runtime."""

from pathlib import Path
import os
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/forgejo_runner/test.py"


class DispatchTests(unittest.TestCase):
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

    def test_host_fixture_requires_an_explicit_disposable_target(self):
        result = self.run_cli("host-fixture")
        self.assertEqual(1, result.returncode)
        self.assertIn("disposable", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
