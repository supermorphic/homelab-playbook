"""Catch unsafe initial-password handling and acceptance of runtime failures."""

import importlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class CompatibilityTests(unittest.TestCase):
    def module(self):
        self.assertTrue((ROOT / "scripts/forgejo/compatibility.py").is_file(),
                        "Forgejo compatibility experiment is missing")
        return importlib.import_module("scripts.forgejo.compatibility")

    def test_initial_random_password_is_parsed_without_logging_it(self):
        module = self.module()
        self.assertEqual("synthetic-only", module.bootstrap_password(
            "generated random password is 'synthetic-only'\nNew user created\n"))

    def test_malformed_bootstrap_output_is_rejected_without_echoing_it(self):
        with self.assertRaises(ValueError) as caught:
            self.module().bootstrap_password("fixture-secret-with-no-frame")
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_random_password_can_contain_quote_characters(self):
        self.assertEqual("synthetic'only", self.module().bootstrap_password(
            "generated random password is 'synthetic'only'\n"))

    def test_database_sql_quotes_password_without_command_arguments(self):
        sql = self.module().database_sql("synthetic'password")
        self.assertIn("PASSWORD 'synthetic''password'", sql)
        self.assertIn("NOSUPERUSER NOCREATEDB NOCREATEROLE", sql)

    def test_database_bootstrap_waits_for_final_server(self):
        module = self.module()

        class StartupRun(module.Run):
            phase = "initializing"

            def create(self, kind, suffix, arguments):
                return self.name(suffix)

            def command(self, argv, *, input_text=None, check=True, timeout=120):
                if "pg_isready" in argv:
                    options = argv[argv.index("pg_isready") + 1:]
                    host = next((options[options.index(flag) + 1]
                                 for flag in ("-h", "--host") if flag in options), None)
                    tcp = host is not None and not host.startswith("/")
                    if tcp and self.phase == "initializing":
                        self.phase = "running"
                        return subprocess.CompletedProcess(argv, 2, "", "")
                    # The image's initialization server already accepts Unix sockets.
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if "psql" in argv and self.phase != "running":
                    raise RuntimeError("database bootstrap reached the initialization server")
                output = "127.0.0.1:18081\n" if argv[1] == "port" else ""
                return subprocess.CompletedProcess(argv, 0, output, "")

        startup = StartupRun(run_id="startup123")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(module.Application, "configuration", return_value=""), \
                patch.object(module.Application, "wait"), \
                patch("scripts.forgejo.runtime.time.sleep"):
            module.Application(startup, Path(directory))
        self.assertEqual("running", startup.phase)


if __name__ == "__main__":
    unittest.main()
