from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_CHECKS_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "repository_checks.py"


def load_repository_checks() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "repository_checks", REPOSITORY_CHECKS_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {REPOSITORY_CHECKS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repository_checks = load_repository_checks()


class RepositoryCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write(self, relative_path: str, content: str, *, mode: int = 0o644) -> Path:
        file_path = self.repo_root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        file_path.chmod(mode)
        return file_path

    def test_json_parser_accepts_valid_json(self) -> None:
        file_path = self.write("config.json", '{"enabled": true}\n')

        errors = repository_checks.check_json(file_path, self.repo_root)

        self.assertEqual([], errors)

    def test_json_parser_reports_relative_path_for_invalid_json(self) -> None:
        file_path = self.write("nested/config.json", '{"private-marker": }\n')

        errors = repository_checks.check_json(file_path, self.repo_root)

        self.assertEqual(["nested/config.json: invalid JSON"], errors)
        self.assertNotIn("private-marker", errors[0])

    def test_toml_parser_accepts_valid_toml(self) -> None:
        file_path = self.write("settings.toml", '[tool]\nenabled = true\n')

        errors = repository_checks.check_toml(file_path, self.repo_root)

        self.assertEqual([], errors)

    def test_toml_parser_reports_relative_path_for_invalid_toml(self) -> None:
        file_path = self.write("nested/settings.toml", '[tool\nprivate-marker = true\n')

        errors = repository_checks.check_toml(file_path, self.repo_root)

        self.assertEqual(["nested/settings.toml: invalid TOML"], errors)
        self.assertNotIn("private-marker", errors[0])

    def test_executable_script_requires_shebang(self) -> None:
        file_path = self.write("missing-shebang.sh", "exit 0\n", mode=0o755)

        errors = repository_checks.check_executable(file_path, self.repo_root)

        self.assertEqual(
            ["missing-shebang.sh: executable file is missing a shebang"], errors
        )

    def test_shebang_script_requires_executable_mode(self) -> None:
        file_path = self.write(
            "not-executable.sh", "#!/usr/bin/env bash\nexit 0\n", mode=0o644
        )

        errors = repository_checks.check_executable(file_path, self.repo_root)

        self.assertEqual(
            ["not-executable.sh: shebang file is not executable"], errors
        )

    def test_executable_shell_script_is_valid(self) -> None:
        file_path = self.write(
            "valid.sh", "#!/usr/bin/env bash\nexit 0\n", mode=0o755
        )

        errors = repository_checks.check_executable(file_path, self.repo_root)

        self.assertEqual([], errors)
        self.assertTrue(os.access(file_path, os.X_OK))

    def test_license_accepts_apache_2_signature(self) -> None:
        self.write(
            "LICENSE",
            "Apache License\nVersion 2.0, January 2004\n",
        )

        errors = repository_checks.check_license(self.repo_root)

        self.assertEqual([], errors)

    def test_license_rejects_wrong_license(self) -> None:
        self.write("LICENSE", "GNU GENERAL PUBLIC LICENSE\nVersion 3\n")

        errors = repository_checks.check_license(self.repo_root)

        self.assertEqual(["LICENSE: missing Apache-2.0 signature"], errors)

    def test_mise_lock_accepts_matching_exact_tool_pin(self) -> None:
        self.write(".mise.toml", '[tools]\npython = "3.13.14"\n')
        self.write(
            "mise.lock",
            '[[tools.python]]\nversion = "3.13.14"\nspecifiers = ["3.13.14"]\n',
        )

        errors = repository_checks.check_mise_lock(self.repo_root)

        self.assertEqual([], errors)

    def test_mise_lock_rejects_mismatched_exact_tool_pin(self) -> None:
        self.write(".mise.toml", '[tools]\npython = "3.13.14"\n')
        self.write(
            "mise.lock",
            '[[tools.python]]\nversion = "3.13.13"\nspecifiers = ["3.13.13"]\n',
        )

        errors = repository_checks.check_mise_lock(self.repo_root)

        self.assertEqual(
            ["mise.lock: exact pin python@3.13.14 is not represented"], errors
        )

    def test_discovery_includes_tracked_and_untracked_nonignored_files(self) -> None:
        subprocess.run(
            ["git", "init", "--quiet"], cwd=self.repo_root, check=True
        )
        tracked = self.write("tracked file.json", "{}\n")
        untracked = self.write("untracked\nfile.toml", "value = true\n")
        self.write("ignored.txt", "ignored\n")
        self.write(".gitignore", "ignored.txt\n")
        subprocess.run(
            ["git", "add", "tracked file.json", ".gitignore"],
            cwd=self.repo_root,
            check=True,
        )

        discovered = repository_checks.discover_repository_files(self.repo_root)

        self.assertIn(tracked, discovered)
        self.assertIn(untracked, discovered)
        self.assertNotIn(self.repo_root / "ignored.txt", discovered)


if __name__ == "__main__":
    unittest.main()
