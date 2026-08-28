from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Iterator, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CLASSIFIER_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "classify.py"
RUN_CHANGED_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "run_changed.py"


def load_module(name: str, path: Path) -> ModuleType:
    if not path.is_file():
        raise RuntimeError(f"required implementation does not exist: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


classifier = load_module("classifier", CLASSIFIER_PATH)


@contextmanager
def working_directory(path: Path) -> Iterator[None]:
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


class TemporaryGitRepository:
    def __init__(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "Classifier Test")
        self.git("config", "user.email", "classifier@example.invalid")

    def cleanup(self) -> None:
        self.temporary_directory.cleanup()

    def git(
        self,
        *arguments: str,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            input=input_text,
        )

    def write(self, relative_path: str, content: str) -> Path:
        file_path = self.root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return file_path

    def commit_all(self, message: str) -> str:
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def initialize_fixture(self) -> str:
        self.write(".gitignore", "ignored.txt\n")
        self.write("README.md", "repository guide\n")
        self.write("docs/old guide.md", "rename fixture\n")
        self.write("roles/update_pihole/tasks/main.yml", "---\n")
        return self.commit_all("base")

    def run_python(
        self,
        script: Path,
        arguments: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *arguments],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )


class PathClassificationTests(unittest.TestCase):
    def test_exact_path_mappings(self) -> None:
        mappings = {
            "README.md": "fast",
            "docs/specs/001-agentic-development-modernization.md": "fast",
            "run-playbook": "fast",
            "scripts/playbook.sh": "fast",
            "inventory/production/hosts.ini": "ansible",
            "inventory/frozen/k3s/hosts.ini": "ansible",
            "playbooks/pihole/update.yml": "ansible",
            "roles/update-pihole/tasks/main.yml": "ansible",
            "ansible.cfg": "ansible",
            ".mise.toml": "full",
            "mise.lock": "full",
            "pyproject.toml": "full",
            "uv.lock": "full",
            "requirements.yml": "full",
            ".github/workflows/ci.yml": "full",
            "scripts/ci/classify.py": "full",
            "scripts/ci/check-fast.sh": "full",
            "tests/ci/test_classify.py": "full",
            "tests/ansible/inventory-test.sh": "full",
            "unknown/new-surface.txt": "full",
        }

        for path, expected_depth in mappings.items():
            with self.subTest(path=path):
                depth, reason = classifier.classify_path(path)
                self.assertEqual(expected_depth, depth)
                self.assertTrue(reason)

    def test_current_security_validation_and_renamed_role_mappings(self) -> None:
        mappings = {
            ".gitleaks.toml": "full",
            ".gitleaksignore": "full",
            ".yamllint": "full",
            "tests/toolchain/test_dependencies.py": "full",
            "scripts/ci/ansible-sources.sh": "full",
            ".ansible-lint": "ansible",
            "roles/enable_cgroup/tasks/main.yml": "ansible",
            "roles/kube_vip/tasks/main.yml": "ansible",
            "roles/prepare_cifs_storage/tasks/main.yml": "ansible",
            "roles/system_maintenance/tasks/main.yml": "ansible",
            "roles/update_pihole/tasks/main.yml": "ansible",
        }

        for path, expected_depth in mappings.items():
            with self.subTest(path=path):
                self.assertEqual(expected_depth, classifier.classify_path(path)[0])

    def test_security_and_validation_configs_have_an_accurate_full_reason(self) -> None:
        expected_reason = (
            "security or validation configuration changes require full validation"
        )

        for path in (
            ".codespellrc",
            ".gitleaks.toml",
            ".gitleaksignore",
            ".markdownlint-cli2.yaml",
            ".pre-commit-config.yaml",
            ".yamllint",
        ):
            with self.subTest(path=path):
                self.assertEqual(("full", expected_reason), classifier.classify_path(path))

    def test_documentation_policy_and_operator_fixtures_are_fast(self) -> None:
        mappings = {
            "AGENTS.md": "fast",
            "CONTRIBUTING.md": "fast",
            "LICENSE": "fast",
            "docs/operations.md": "fast",
            "tests/operator/run-playbook-test.sh": "fast",
        }

        for path, expected_depth in mappings.items():
            with self.subTest(path=path):
                self.assertEqual(expected_depth, classifier.classify_path(path)[0])

    def test_deepest_mapping_wins_and_paths_are_deduplicated_and_sorted(self) -> None:
        result = classifier.classify_paths(
            [
                "roles/update_pihole/tasks/main.yml",
                "README.md",
                "scripts/ci/classify.py",
                "README.md",
            ]
        )

        self.assertEqual("full", result["depth"])
        self.assertTrue(result["run_fast"])
        self.assertTrue(result["run_ansible"])
        self.assertFalse(result["run_molecule"])
        self.assertEqual(
            [
                "README.md",
                "roles/update_pihole/tasks/main.yml",
                "scripts/ci/classify.py",
            ],
            result["paths"],
        )
        self.assertEqual(set(result["paths"]), set(result["reasons"]))

    def test_empty_or_invalid_path_results_fail_closed(self) -> None:
        empty_result = classifier.classify_paths([])
        invalid_result = classifier.classify_paths([""])

        self.assertEqual("full", empty_result["depth"])
        self.assertEqual([], empty_result["paths"])
        self.assertEqual(
            {"<classifier>": "no changed paths were discovered; full validation required"},
            empty_result["reasons"],
        )
        self.assertEqual("full", invalid_result["depth"])
        self.assertEqual([""], invalid_result["paths"])

    def test_reserved_molecule_depth_is_declared_but_never_selected(self) -> None:
        self.assertEqual(
            {"fast": 0, "ansible": 1, "molecule": 2, "full": 3},
            classifier.DEPTH_ORDER,
        )

        for paths in (["README.md"], ["roles/update_pihole/tasks/main.yml"], []):
            with self.subTest(paths=paths):
                result = classifier.classify_paths(list(paths))
                self.assertNotEqual("molecule", result["depth"])
                self.assertFalse(result["run_molecule"])


class OutputFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.special_path = "docs/line\nbreak%file.md"
        self.result = classifier.classify_paths([self.special_path])

    def test_json_output_is_deterministic_and_machine_readable(self) -> None:
        first = classifier.format_json(self.result)
        second = classifier.format_json(self.result)

        self.assertEqual(first, second)
        self.assertEqual(self.result, json.loads(first))
        self.assertIn("\\n", first)
        self.assertIn("%", first)
        self.assertNotIn(self.special_path, first)

    def test_text_output_escapes_multiline_paths(self) -> None:
        output = classifier.format_text(self.result)

        self.assertEqual(
            "\n".join(
                [
                    "depth: fast",
                    "run_fast: true",
                    "run_ansible: false",
                    "run_molecule: false",
                    "paths:",
                    '  "docs/line\\nbreak%file.md": '
                    "documentation, license, or policy changes require fast validation",
                ]
            ),
            output,
        )
        self.assertNotIn(self.special_path, output)

    def test_github_output_uses_stable_single_line_json_values(self) -> None:
        output = classifier.format_github(self.result)

        self.assertEqual(
            "\n".join(
                [
                    "depth=fast",
                    "run_fast=true",
                    "run_ansible=false",
                    "run_molecule=false",
                    'paths=["docs/line\\nbreak%file.md"]',
                    'reasons={"docs/line\\nbreak%file.md":'
                    '"documentation, license, or policy changes require fast validation"}',
                ]
            ),
            output,
        )
        self.assertEqual(6, len(output.splitlines()))
        self.assertIn("%", output)
        self.assertNotIn(self.special_path, output)


class GitOutputParserTests(unittest.TestCase):
    def test_name_status_accepts_complete_one_and_two_path_records(self) -> None:
        output = (
            b"M\0docs/guide.md\0"
            b"R057\0docs/old.md\0docs/new.md\0"
            b"C087\0roles/source.yml\0docs/copy.yml\0"
        )

        self.assertEqual(
            [
                "docs/guide.md",
                "docs/old.md",
                "docs/new.md",
                "roles/source.yml",
                "docs/copy.yml",
            ],
            classifier._parse_name_status(output),
        )
        self.assertEqual([], classifier._parse_name_status(b""))

    def test_name_status_rejects_every_malformed_record_shape(self) -> None:
        malformed_outputs = {
            "missing final NUL": b"M\0docs/guide.md",
            "empty status": b"\0",
            "empty path": b"M\0\0",
            "one-path status suffix": b"Mgarbage\0docs/guide.md\0",
            "added status suffix": b"A100\0docs/guide.md\0",
            "rename missing score": b"R\0docs/old.md\0docs/new.md\0",
            "rename non-padded score": b"R57\0docs/old.md\0docs/new.md\0",
            "rename invalid score": b"Rgarbage\0docs/old.md\0docs/new.md\0",
            "copy score above 100": b"C101\0roles/source.yml\0docs/copy.yml\0",
            "truncated rename": b"R100\0docs/old.md\0",
            "truncated copy": b"C100\0roles/source.yml\0",
            "stray field": b"M\0docs/guide.md\0stray\0",
        }

        for name, output in malformed_outputs.items():
            with self.subTest(name=name):
                with self.assertRaises(classifier.GitDiscoveryError):
                    classifier._parse_name_status(output)

    def test_untracked_parser_accepts_only_complete_nonempty_path_records(self) -> None:
        output = b"docs/guide.md\0docs/line\nbreak%file.md\0"

        self.assertEqual(
            ["docs/guide.md", "docs/line\nbreak%file.md"],
            classifier._parse_nul_paths(output),
        )
        self.assertEqual([], classifier._parse_nul_paths(b""))

        malformed_outputs = {
            "missing final NUL": b"docs/guide.md",
            "empty path": b"\0",
            "empty trailing record": b"docs/guide.md\0\0",
        }
        for name, malformed_output in malformed_outputs.items():
            with self.subTest(name=name):
                with self.assertRaises(classifier.GitDiscoveryError):
                    classifier._parse_nul_paths(malformed_output)


class GitDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = TemporaryGitRepository()
        self.base = self.repository.initialize_fixture()

    def tearDown(self) -> None:
        self.repository.cleanup()

    def test_committed_diff_includes_deletion_and_both_rename_paths(self) -> None:
        self.repository.git("mv", "docs/old guide.md", "docs/new guide.md")
        (self.repository.root / "roles/update_pihole/tasks/main.yml").unlink()
        self.repository.write("inventory/production/new hosts.ini", "[pihole]\n")
        self.repository.commit_all("committed changes")

        with working_directory(self.repository.root):
            paths = classifier.discover_changes(self.base, "HEAD", False)

        self.assertEqual(
            [
                "docs/new guide.md",
                "docs/old guide.md",
                "inventory/production/new hosts.ini",
                "roles/update_pihole/tasks/main.yml",
            ],
            paths,
        )

    def test_committed_copy_includes_unchanged_source_and_deepest_wins(self) -> None:
        self.repository.write("docs/copied-role.yml", "---\n")
        self.repository.commit_all("copy unchanged role source")

        with working_directory(self.repository.root):
            paths = classifier.discover_changes(self.base, "HEAD", False)

        self.assertEqual(
            ["docs/copied-role.yml", "roles/update_pihole/tasks/main.yml"],
            paths,
        )
        self.assertEqual("ansible", classifier.classify_paths(paths)["depth"])

    def test_staged_copy_includes_unchanged_source_and_deepest_wins(self) -> None:
        self.repository.write("docs/staged-copied-role.yml", "---\n")
        self.repository.git("add", "docs/staged-copied-role.yml")

        with working_directory(self.repository.root):
            paths = classifier.discover_changes("HEAD", "HEAD", True)

        self.assertEqual(
            [
                "docs/staged-copied-role.yml",
                "roles/update_pihole/tasks/main.yml",
            ],
            paths,
        )
        self.assertEqual("ansible", classifier.classify_paths(paths)["depth"])

    def test_worktree_discovery_unions_committed_staged_unstaged_and_untracked(self) -> None:
        self.repository.write("docs/committed.md", "committed\n")
        self.repository.commit_all("candidate")
        self.repository.git("mv", "README.md", "README renamed.md")
        self.repository.write("inventory/staging/new hosts.yml", "all: {}\n")
        self.repository.git("add", "inventory/staging/new hosts.yml")
        self.repository.write(
            "roles/update_pihole/tasks/main.yml", "---\n# unstaged\n"
        )
        self.repository.write("playbooks/pihole/untracked.yml", "---\n")
        self.repository.write("docs/line\nbreak%file.md", "special\n")
        self.repository.write("ignored.txt", "ignored\n")

        with working_directory(self.repository.root):
            committed_only = classifier.discover_changes(self.base, "HEAD", False)
            with_worktree = classifier.discover_changes(self.base, "HEAD", True)

        self.assertEqual(["docs/committed.md"], committed_only)
        self.assertEqual(
            [
                "README renamed.md",
                "README.md",
                "docs/committed.md",
                "docs/line\nbreak%file.md",
                "inventory/staging/new hosts.yml",
                "playbooks/pihole/untracked.yml",
                "roles/update_pihole/tasks/main.yml",
            ],
            with_worktree,
        )
        self.assertNotIn("ignored.txt", with_worktree)

    def test_missing_merge_base_selects_full(self) -> None:
        tree = self.repository.git("rev-parse", "HEAD^{tree}").stdout.strip()
        unrelated_head = self.repository.git(
            "commit-tree", tree, input_text="unrelated\n"
        ).stdout.strip()

        result = self.repository.run_python(
            CLASSIFIER_PATH,
            ["--base", self.base, "--head", unrelated_head, "--format", "json"],
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("full", payload["depth"])
        self.assertEqual([], payload["paths"])
        self.assertEqual(
            {"<classifier>": "Git change discovery failed; full validation required"},
            payload["reasons"],
        )

    def test_invalid_revision_selects_full(self) -> None:
        result = self.repository.run_python(
            CLASSIFIER_PATH,
            ["--base", "does-not-exist", "--head", "HEAD", "--format", "json"],
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("full", payload["depth"])
        self.assertTrue(payload["run_fast"])
        self.assertTrue(payload["run_ansible"])
        self.assertFalse(payload["run_molecule"])

    def test_git_error_outside_repository_selects_full(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = subprocess.run(
                [sys.executable, str(CLASSIFIER_PATH), "--format", "json"],
                cwd=temporary_directory,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("full", json.loads(result.stdout)["depth"])

    def test_malformed_successful_git_output_selects_full(self) -> None:
        fake_git_source = """#!/usr/bin/env python3
import os
import sys

command = sys.argv[1]
if command in {"rev-parse", "merge-base"}:
    sys.stdout.write("a" * 40 + "\\n")
elif command == "diff":
    sys.stdout.buffer.write(bytes.fromhex(os.environ["FAKE_DIFF_HEX"]))
elif command == "ls-files":
    sys.stdout.buffer.write(bytes.fromhex(os.environ["FAKE_UNTRACKED_HEX"]))
else:
    raise SystemExit(1)
"""
        cases = {
            "unterminated diff": {
                "diff": b"M\0docs/partial.md",
                "untracked": b"",
                "arguments": [],
            },
            "unterminated untracked": {
                "diff": b"",
                "untracked": b"docs/partial.md",
                "arguments": ["--include-worktree"],
            },
        }

        for name, case in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                temporary_root = Path(temporary)
                fake_bin = temporary_root / "bin"
                fake_bin.mkdir()
                fake_git = fake_bin / "git"
                fake_git.write_text(fake_git_source, encoding="utf-8")
                fake_git.chmod(0o755)
                environment = os.environ.copy()
                environment.update(
                    {
                        "PATH": f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}",
                        "FAKE_DIFF_HEX": case["diff"].hex(),
                        "FAKE_UNTRACKED_HEX": case["untracked"].hex(),
                    }
                )

                result = subprocess.run(
                    [
                        sys.executable,
                        str(CLASSIFIER_PATH),
                        "--base",
                        "base",
                        "--head",
                        "head",
                        *case["arguments"],
                        "--format",
                        "json",
                    ],
                    cwd=temporary_root,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(0, result.returncode, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual("full", payload["depth"])
                self.assertEqual([], payload["paths"])
                self.assertEqual(
                    {
                        "<classifier>": (
                            "Git change discovery failed; full validation required"
                        )
                    },
                    payload["reasons"],
                )


class ClassifierCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = TemporaryGitRepository()
        self.repository.initialize_fixture()

    def tearDown(self) -> None:
        self.repository.cleanup()

    def test_force_depth_escalates_fast_result_to_full(self) -> None:
        self.repository.write("docs/new.md", "documentation\n")

        result = self.repository.run_python(
            CLASSIFIER_PATH,
            [
                "--base",
                "HEAD",
                "--include-worktree",
                "--force-depth",
                "full",
                "--format",
                "json",
            ],
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("full", payload["depth"])
        self.assertTrue(payload["run_ansible"])

    def test_force_depth_rejects_deescalation(self) -> None:
        self.repository.write("roles/update_pihole/tasks/new.yml", "---\n")

        result = self.repository.run_python(
            CLASSIFIER_PATH,
            [
                "--base",
                "HEAD",
                "--include-worktree",
                "--force-depth",
                "fast",
                "--format",
                "json",
            ],
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("cannot de-escalate from ansible to fast", result.stderr)
        self.assertEqual("", result.stdout)

    def test_force_depth_rejects_reserved_molecule(self) -> None:
        result = self.repository.run_python(
            CLASSIFIER_PATH,
            ["--base", "HEAD", "--force-depth", "molecule"],
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("invalid choice: 'molecule'", result.stderr)


class ChangedRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = TemporaryGitRepository()
        self.repository.initialize_fixture()

    def tearDown(self) -> None:
        self.repository.cleanup()

    def run_changed(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.repository.run_python(RUN_CHANGED_PATH, list(arguments))

    def test_fast_dry_run_explains_path_and_prints_only_fast_command(self) -> None:
        self.repository.write("docs/new guide.md", "documentation\n")

        result = self.run_changed("--dry-run", "--base", "HEAD")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Selected validation depth: fast", result.stdout)
        self.assertIn(
            '"docs/new guide.md": documentation, license, or policy changes '
            "require fast validation",
            result.stdout,
        )
        self.assertEqual(1, result.stdout.count("Would run:"))
        self.assertIn("Would run: mise run check:fast", result.stdout)
        self.assertNotIn("check:ansible", result.stdout)

    def test_ansible_dry_run_prints_fast_then_ansible_commands(self) -> None:
        self.repository.write("roles/update_pihole/tasks/new.yml", "---\n")

        result = self.run_changed("--dry-run", "--base", "HEAD")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Selected validation depth: ansible", result.stdout)
        self.assertEqual(2, result.stdout.count("Would run:"))
        self.assertLess(
            result.stdout.index("Would run: mise run check:fast"),
            result.stdout.index("Would run: mise run check:ansible"),
        )

    def test_dry_run_may_escalate_to_full(self) -> None:
        self.repository.write("docs/new.md", "documentation\n")

        result = self.run_changed(
            "--dry-run", "--base", "HEAD", "--force-depth", "full"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Selected validation depth: full", result.stdout)
        self.assertIn("Escalated validation depth: fast -> full", result.stdout)
        self.assertEqual(2, result.stdout.count("Would run:"))

    def test_dry_run_rejects_requested_deescalation(self) -> None:
        self.repository.write("roles/update_pihole/tasks/new.yml", "---\n")

        result = self.run_changed(
            "--dry-run", "--base", "HEAD", "--force-depth", "fast"
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("cannot de-escalate from ansible to fast", result.stderr)
        self.assertNotIn("Would run:", result.stdout)

    def test_runner_rejects_reserved_molecule(self) -> None:
        result = self.run_changed(
            "--dry-run", "--base", "HEAD", "--force-depth", "molecule"
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("invalid choice: 'molecule'", result.stderr)


if __name__ == "__main__":
    unittest.main()
