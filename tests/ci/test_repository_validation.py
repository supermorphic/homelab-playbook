from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_VALIDATION_PATH = (
    REPOSITORY_ROOT / "scripts" / "ci" / "repository_validation.py"
)


def load_repository_validation() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "repository_validation", REPOSITORY_VALIDATION_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {REPOSITORY_VALIDATION_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repository_validation = load_repository_validation()


class RepositoryValidationTests(unittest.TestCase):
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

        errors = repository_validation.validate_json(file_path, self.repo_root)

        self.assertEqual([], errors)

    def test_json_parser_reports_relative_path_for_invalid_json(self) -> None:
        file_path = self.write("nested/config.json", '{"private-marker": }\n')

        errors = repository_validation.validate_json(file_path, self.repo_root)

        self.assertEqual(["nested/config.json: invalid JSON"], errors)
        self.assertNotIn("private-marker", errors[0])

    def test_toml_parser_accepts_valid_toml(self) -> None:
        file_path = self.write("settings.toml", '[tool]\nenabled = true\n')

        errors = repository_validation.validate_toml(file_path, self.repo_root)

        self.assertEqual([], errors)

    def test_toml_parser_reports_relative_path_for_invalid_toml(self) -> None:
        file_path = self.write("nested/settings.toml", '[tool\nprivate-marker = true\n')

        errors = repository_validation.validate_toml(file_path, self.repo_root)

        self.assertEqual(["nested/settings.toml: invalid TOML"], errors)
        self.assertNotIn("private-marker", errors[0])

    def test_executable_script_requires_shebang(self) -> None:
        file_path = self.write("missing-shebang.sh", "exit 0\n", mode=0o755)

        errors = repository_validation.validate_executable(file_path, self.repo_root)

        self.assertEqual(
            ["missing-shebang.sh: executable file is missing a shebang"], errors
        )

    def test_shebang_script_requires_executable_mode(self) -> None:
        file_path = self.write(
            "not-executable.sh", "#!/usr/bin/env bash\nexit 0\n", mode=0o644
        )

        errors = repository_validation.validate_executable(file_path, self.repo_root)

        self.assertEqual(
            ["not-executable.sh: shebang file is not executable"], errors
        )

    def test_executable_shell_script_is_valid(self) -> None:
        file_path = self.write(
            "valid.sh", "#!/usr/bin/env bash\nexit 0\n", mode=0o755
        )

        errors = repository_validation.validate_executable(file_path, self.repo_root)

        self.assertEqual([], errors)
        self.assertTrue(os.access(file_path, os.X_OK))

    def test_license_accepts_apache_2_signature(self) -> None:
        self.write(
            "LICENSE",
            "Apache License\nVersion 2.0, January 2004\n",
        )

        errors = repository_validation.validate_license(self.repo_root)

        self.assertEqual([], errors)

    def test_license_rejects_wrong_license(self) -> None:
        self.write("LICENSE", "GNU GENERAL PUBLIC LICENSE\nVersion 3\n")

        errors = repository_validation.validate_license(self.repo_root)

        self.assertEqual(["LICENSE: missing Apache-2.0 signature"], errors)

    def test_mise_lock_accepts_matching_exact_tool_pin(self) -> None:
        self.write(".mise.toml", '[tools]\npython = "3.13.14"\n')
        self.write(
            "mise.lock",
            '[[tools.python]]\nversion = "3.13.14"\nspecifiers = ["3.13.14"]\n',
        )

        errors = repository_validation.validate_mise_lock(self.repo_root)

        self.assertEqual([], errors)

    def test_mise_lock_rejects_mismatched_exact_tool_pin(self) -> None:
        self.write(".mise.toml", '[tools]\npython = "3.13.14"\n')
        self.write(
            "mise.lock",
            '[[tools.python]]\nversion = "3.13.13"\nspecifiers = ["3.13.13"]\n',
        )

        errors = repository_validation.validate_mise_lock(self.repo_root)

        self.assertEqual(
            [
                "mise.lock: exact pin python@3.13.14 is not represented; "
                "run mise run bootstrap"
            ],
            errors,
        )

    def test_mise_lock_rejects_floating_tool_specs(self) -> None:
        self.write(
            ".mise.toml",
            '[tools]\npython = "latest"\nuv = "0.11"\nnode = "^24.18.0"\n',
        )
        self.write("mise.lock", "lockfile_version = 1\n[tools]\n")

        errors = repository_validation.validate_mise_lock(self.repo_root)

        self.assertEqual(
            [
                ".mise.toml: tool node must use an exact version; "
                "run mise run bootstrap",
                ".mise.toml: tool python must use an exact version; "
                "run mise run bootstrap",
                ".mise.toml: tool uv must use an exact version; "
                "run mise run bootstrap",
            ],
            errors,
        )

    def test_mise_lock_requires_requested_version_in_lock_specifiers(self) -> None:
        self.write(".mise.toml", '[tools]\npython = "3.13.14"\n')
        self.write(
            "mise.lock",
            '[[tools.python]]\nversion = "3.13.14"\nspecifiers = ["latest"]\n',
        )

        errors = repository_validation.validate_mise_lock(self.repo_root)

        self.assertEqual(
            [
                "mise.lock: exact pin python@3.13.14 is not represented; "
                "run mise run bootstrap"
            ],
            errors,
        )

    def test_mise_lock_accepts_reviewed_trust_policy_exception(self) -> None:
        self.write(
            ".mise.toml",
            "[tools]\n"
            '"npm:markdownlint-cli2" = { version = "0.23.2", '
            'trust_policy_excludes = ["fastq@1.20.2"] }\n',
        )
        self.write(
            "mise.lock",
            """[[tools."npm:markdownlint-cli2"]]
version = "0.23.2"
backend = "npm:markdownlint-cli2"
specifiers = ["0.23.2"]
options = { trust_policy_excludes = '["fastq@1.20.2"]' }
""",
        )

        errors = repository_validation.validate_mise_lock(self.repo_root)

        self.assertEqual([], errors)

    def test_mise_lock_rejects_non_exact_reviewed_tool_identity(self) -> None:
        invalid_identities = {
            "version drift": (
                'version = "0.23.3", '
                'trust_policy_excludes = ["fastq@1.20.2"]',
                "0.23.3",
                "options = { trust_policy_excludes = "
                "'[\"fastq@1.20.2\"]' }\n",
            ),
            "missing exception": (
                'version = "0.23.2"',
                "0.23.2",
                "",
            ),
        }

        for case, (tool_options, version, lock_options) in invalid_identities.items():
            with self.subTest(case=case):
                self.write(
                    ".mise.toml",
                    "[tools]\n"
                    f'"npm:markdownlint-cli2" = {{ {tool_options} }}\n',
                )
                self.write(
                    "mise.lock",
                    """[[tools."npm:markdownlint-cli2"]]
"""
                    + f'version = "{version}"\n'
                    + 'backend = "npm:markdownlint-cli2"\n'
                    + f'specifiers = ["{version}"]\n'
                    + lock_options,
                )

                errors = repository_validation.validate_mise_lock(self.repo_root)

                self.assertEqual(
                    [
                        ".mise.toml: tool npm:markdownlint-cli2 has an "
                        "unapproved trust_policy_excludes value; "
                        "run mise run bootstrap"
                    ],
                    errors,
                )

    def test_mise_lock_rejects_extra_reviewed_tool_options(self) -> None:
        extra_options = {
            "trust related": "allow_low_downloads = true",
            "release age": 'minimum_release_age = "0d"',
            "package manager": 'npm_args = "--ignore-scripts"',
            "arbitrary": "future_option = true",
        }

        for case, extra_option in extra_options.items():
            with self.subTest(case=case):
                self.write(
                    ".mise.toml",
                    "[tools]\n"
                    '"npm:markdownlint-cli2" = { version = "0.23.2", '
                    'trust_policy_excludes = ["fastq@1.20.2"], '
                    f"{extra_option} }}\n",
                )
                self.write(
                    "mise.lock",
                    """[[tools."npm:markdownlint-cli2"]]
version = "0.23.2"
backend = "npm:markdownlint-cli2"
specifiers = ["0.23.2"]
options = { trust_policy_excludes = '["fastq@1.20.2"]' }
""",
                )

                errors = repository_validation.validate_mise_lock(self.repo_root)

                self.assertEqual(
                    [
                        ".mise.toml: tool npm:markdownlint-cli2 has an "
                        "unapproved trust_policy_excludes value; "
                        "run mise run bootstrap"
                    ],
                    errors,
                )

    def test_mise_lock_requires_exact_reviewed_backend(self) -> None:
        invalid_backends = {
            "missing": "",
            "wrong": 'backend = "npm:other-tool"\n',
        }

        for case, backend in invalid_backends.items():
            with self.subTest(case=case):
                self.write(
                    ".mise.toml",
                    "[tools]\n"
                    '"npm:markdownlint-cli2" = { version = "0.23.2", '
                    'trust_policy_excludes = ["fastq@1.20.2"] }\n',
                )
                self.write(
                    "mise.lock",
                    """[[tools."npm:markdownlint-cli2"]]
version = "0.23.2"
"""
                    + backend
                    + """specifiers = ["0.23.2"]
options = { trust_policy_excludes = '["fastq@1.20.2"]' }
""",
                )

                errors = repository_validation.validate_mise_lock(self.repo_root)

                self.assertEqual(
                    [
                        "mise.lock: trust_policy_excludes for "
                        "npm:markdownlint-cli2@0.23.2 is not represented; "
                        "run mise run bootstrap"
                    ],
                    errors,
                )

    def test_mise_lock_rejects_extra_reviewed_lock_options(self) -> None:
        extra_options = {
            "trust related": 'allow_low_downloads = "true"',
            "release age": 'minimum_release_age = "0d"',
            "package manager": 'npm_args = "--ignore-scripts"',
            "arbitrary": 'future_option = "true"',
        }

        for case, extra_option in extra_options.items():
            with self.subTest(case=case):
                self.write(
                    ".mise.toml",
                    "[tools]\n"
                    '"npm:markdownlint-cli2" = { version = "0.23.2", '
                    'trust_policy_excludes = ["fastq@1.20.2"] }\n',
                )
                self.write(
                    "mise.lock",
                    """[[tools."npm:markdownlint-cli2"]]
version = "0.23.2"
backend = "npm:markdownlint-cli2"
specifiers = ["0.23.2"]
[tools."npm:markdownlint-cli2".options]
trust_policy_excludes = '["fastq@1.20.2"]'
"""
                    + f"{extra_option}\n",
                )

                errors = repository_validation.validate_mise_lock(self.repo_root)

                self.assertEqual(
                    [
                        "mise.lock: trust_policy_excludes for "
                        "npm:markdownlint-cli2@0.23.2 is not represented; "
                        "run mise run bootstrap"
                    ],
                    errors,
                )

    def test_mise_lock_rejects_broadened_trust_policy_exception(self) -> None:
        invalid_exceptions = {
            "bare package": '["fastq"]',
            "version range": '["fastq@^1.20"]',
            "additional package": '["fastq@1.20.2", "queue@1.0.0"]',
            "additional version": '["fastq@1.20.2", "fastq@1.20.3"]',
        }

        for case, trust_policy_excludes in invalid_exceptions.items():
            with self.subTest(case=case):
                self.write(
                    ".mise.toml",
                    "[tools]\n"
                    '"npm:markdownlint-cli2" = { '
                    'version = "0.23.2", '
                    f"trust_policy_excludes = {trust_policy_excludes} "
                    "}\n",
                )
                self.write(
                    "mise.lock",
                    """[[tools."npm:markdownlint-cli2"]]
version = "0.23.2"
specifiers = ["0.23.2"]
""",
                )

                errors = repository_validation.validate_mise_lock(self.repo_root)

                self.assertEqual(
                    [
                        ".mise.toml: tool npm:markdownlint-cli2 has an "
                        "unapproved trust_policy_excludes value; "
                        "run mise run bootstrap"
                    ],
                    errors,
                )

    def test_mise_lock_rejects_malformed_trust_policy_exception(self) -> None:
        malformed_exceptions = {
            "string": '"fastq@1.20.2"',
            "non-string item": '["fastq@1.20.2", 2]',
            "table": '{ package = "fastq@1.20.2" }',
        }

        for case, trust_policy_excludes in malformed_exceptions.items():
            with self.subTest(case=case):
                self.write(
                    ".mise.toml",
                    "[tools]\n"
                    '"npm:markdownlint-cli2" = { '
                    'version = "0.23.2", '
                    f"trust_policy_excludes = {trust_policy_excludes} "
                    "}\n",
                )
                self.write(
                    "mise.lock",
                    """[[tools."npm:markdownlint-cli2"]]
version = "0.23.2"
specifiers = ["0.23.2"]
""",
                )

                errors = repository_validation.validate_mise_lock(self.repo_root)

                self.assertEqual(
                    [
                        ".mise.toml: tool npm:markdownlint-cli2 has an "
                        "unapproved trust_policy_excludes value; "
                        "run mise run bootstrap"
                    ],
                    errors,
                )

    def test_mise_lock_rejects_trust_policy_exception_on_another_tool(self) -> None:
        self.write(
            ".mise.toml",
            "[tools]\n"
            '"npm:other-tool" = { version = "1.2.3", '
            'trust_policy_excludes = ["fastq@1.20.2"] }\n',
        )
        self.write(
            "mise.lock",
            """[[tools."npm:other-tool"]]
version = "1.2.3"
specifiers = ["1.2.3"]
options = { trust_policy_excludes = '["fastq@1.20.2"]' }
""",
        )

        errors = repository_validation.validate_mise_lock(self.repo_root)

        self.assertEqual(
            [
                ".mise.toml: tool npm:other-tool has an unapproved "
                "trust_policy_excludes value; run mise run bootstrap"
            ],
            errors,
        )

    def test_mise_lock_requires_trust_policy_exception_option(self) -> None:
        self.write(
            ".mise.toml",
            "[tools]\n"
            '"npm:markdownlint-cli2" = { version = "0.23.2", '
            'trust_policy_excludes = ["fastq@1.20.2"] }\n',
        )
        stale_lock_options = {
            "missing option": "",
            "wrong package version": (
                "options = { trust_policy_excludes = "
                "'[\"fastq@1.20.3\"]' }\n"
            ),
        }

        for case, options in stale_lock_options.items():
            with self.subTest(case=case):
                self.write(
                    "mise.lock",
                    """[[tools."npm:markdownlint-cli2"]]
version = "0.23.2"
specifiers = ["0.23.2"]
"""
                    + options,
                )

                errors = repository_validation.validate_mise_lock(self.repo_root)

                self.assertEqual(
                    [
                        "mise.lock: trust_policy_excludes for "
                        "npm:markdownlint-cli2@0.23.2 is not represented; "
                        "run mise run bootstrap"
                    ],
                    errors,
                )

    def test_mise_lock_rejects_obsolete_optionless_trust_policy_request(
        self,
    ) -> None:
        self.write(
            ".mise.toml",
            "[tools]\n"
            '"npm:markdownlint-cli2" = { version = "0.23.2", '
            'trust_policy_excludes = ["fastq@1.20.2"] }\n',
        )
        self.write(
            "mise.lock",
            """[[tools."npm:markdownlint-cli2"]]
version = "0.23.2"
specifiers = ["0.23.2"]

[[tools."npm:markdownlint-cli2"]]
version = "0.23.2"
specifiers = ["0.23.2"]
options = { trust_policy_excludes = '["fastq@1.20.2"]' }
""",
        )

        errors = repository_validation.validate_mise_lock(self.repo_root)

        self.assertEqual(
            [
                "mise.lock: trust_policy_excludes for "
                "npm:markdownlint-cli2@0.23.2 is not represented; "
                "run mise run bootstrap"
            ],
            errors,
        )

    def test_mise_lock_parse_failure_includes_bootstrap_recovery(self) -> None:
        self.write(".mise.toml", '[tools\npython = "3.13.14"\n')
        self.write("mise.lock", "lockfile_version = 1\n")

        errors = repository_validation.validate_mise_lock(self.repo_root)

        self.assertEqual(
            [
                "mise.lock: cannot verify exact tool pins; "
                "run mise run bootstrap"
            ],
            errors,
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

        discovered = repository_validation.discover_repository_files(self.repo_root)

        self.assertIn(tracked, discovered)
        self.assertIn(untracked, discovered)
        self.assertNotIn(self.repo_root / "ignored.txt", discovered)


if __name__ == "__main__":
    unittest.main()
