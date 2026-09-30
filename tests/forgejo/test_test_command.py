"""Catch invalid dispatcher acceptance and missing public help."""

from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
COMMAND = ROOT / "scripts/forgejo/test.py"


class TestCommand(unittest.TestCase):
    def invoke(self, *arguments):
        self.assertTrue(COMMAND.is_file(), "Forgejo test dispatcher is missing")
        return subprocess.run(
            [sys.executable, str(COMMAND), *arguments],
            capture_output=True, text=True, timeout=10, check=False,
        )

    def test_help_describes_supported_modes(self):
        result = self.invoke("--help")
        self.assertEqual(0, result.returncode)
        for mode in ("unit", "compatibility", "fixture", "restore"):
            self.assertIn(mode, result.stdout)

    def test_unknown_mode_is_usage_error(self):
        self.assertEqual(2, self.invoke("repair").returncode)

    def test_extra_arguments_are_usage_error(self):
        self.assertEqual(2, self.invoke("compatibility", "--repair").returncode)


if __name__ == "__main__":
    unittest.main()
