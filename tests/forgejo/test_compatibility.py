"""Catch unsafe initial-password handling and acceptance of runtime failures."""

import importlib
from pathlib import Path
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
