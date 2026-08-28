from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_CHECKS_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "candidate_checks.py"


class TemporaryGitRepository:
    def __init__(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "Candidate Check Test")
        self.git("config", "user.email", "candidate-check@example.invalid")

    def cleanup(self) -> None:
        self.temporary_directory.cleanup()

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
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


def generated_secret() -> str:
    return hashlib.sha256(b"isolated candidate secret fixture").hexdigest()


class CandidateCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = TemporaryGitRepository()
        self.repository.write("README.md", "candidate fixture\n")
        self.base = self.repository.commit_all("base")

    def tearDown(self) -> None:
        self.repository.cleanup()

    def run_checks(
        self, **environment_overrides: str
    ) -> subprocess.CompletedProcess[str]:
        self.assertTrue(
            CANDIDATE_CHECKS_PATH.is_file(),
            "the canonical candidate validator must exist",
        )
        environment = os.environ.copy()
        for variable in (
            "CI_BASE_SHA",
            "CI_HEAD_SHA",
            "FULL_SECRET_SCAN",
            "LOCAL_CHANGE_DIRECTED",
        ):
            environment.pop(variable, None)
        environment.update(environment_overrides)
        return subprocess.run(
            [
                sys.executable,
                str(CANDIDATE_CHECKS_PATH),
                "--repo-root",
                os.fspath(self.repository.root),
            ],
            cwd=self.repository.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def assert_trailing_whitespace_failure(
        self, result: subprocess.CompletedProcess[str], relative_path: str
    ) -> None:
        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            f"{relative_path}:1: trailing whitespace.",
            result.stdout + result.stderr,
        )

    def assert_redacted_secret_failure(
        self, result: subprocess.CompletedProcess[str], secret: str
    ) -> None:
        self.assertNotEqual(0, result.returncode)
        combined_output = result.stdout + result.stderr
        if secret in combined_output:
            self.fail("candidate validation exposed the redacted fixture value")
        self.assertIn("leaks found", combined_output.lower())

    def introduce_and_remove_secret(self, secret: str) -> str:
        self.repository.write("candidate.env", f'api_key = "{secret}"\n')
        self.repository.commit_all("introduce redacted fixture")
        (self.repository.root / "candidate.env").unlink()
        return self.repository.commit_all("remove redacted fixture")

    def test_pr_range_rejects_committed_trailing_whitespace(self) -> None:
        self.repository.write("candidate.txt", "trailing whitespace \n")
        head = self.repository.commit_all("add whitespace")

        result = self.run_checks(CI_BASE_SHA=self.base, CI_HEAD_SHA=head)

        self.assert_trailing_whitespace_failure(result, "candidate.txt")

    def test_pr_range_starts_at_merge_base_not_the_supplied_base_tip(self) -> None:
        self.repository.write("shared.txt", "preexisting whitespace \n")
        self.repository.commit_all("add preexisting whitespace")
        self.repository.git("switch", "--quiet", "-c", "candidate")
        self.repository.write("candidate.txt", "clean candidate\n")
        candidate_head = self.repository.commit_all("add clean candidate")
        self.repository.git("switch", "--quiet", "main")
        self.repository.write("shared.txt", "fixed on base branch\n")
        supplied_base = self.repository.commit_all("fix base branch whitespace")

        result = self.run_checks(
            CI_BASE_SHA=supplied_base,
            CI_HEAD_SHA=candidate_head,
        )

        self.assertEqual(0, result.returncode, result.stderr)

    def test_local_change_directed_rejects_committed_trailing_whitespace(
        self,
    ) -> None:
        self.repository.write("candidate.txt", "trailing whitespace \n")
        head = self.repository.commit_all("add whitespace")

        result = self.run_checks(
            CI_BASE_SHA=self.base,
            CI_HEAD_SHA=head,
            LOCAL_CHANGE_DIRECTED="1",
        )

        self.assert_trailing_whitespace_failure(result, "candidate.txt")

    def test_local_change_directed_rejects_staged_trailing_whitespace(self) -> None:
        self.repository.write("candidate.txt", "trailing whitespace \n")
        self.repository.git("add", "candidate.txt")

        result = self.run_checks(
            CI_BASE_SHA=self.base,
            CI_HEAD_SHA=self.base,
            LOCAL_CHANGE_DIRECTED="1",
        )

        self.assert_trailing_whitespace_failure(result, "candidate.txt")

    def test_local_change_directed_rejects_unstaged_trailing_whitespace(self) -> None:
        self.repository.write("candidate.txt", "clean\n")
        self.repository.commit_all("add clean candidate")
        self.repository.write("candidate.txt", "trailing whitespace \n")
        head = self.repository.git("rev-parse", "HEAD").stdout.strip()

        result = self.run_checks(
            CI_BASE_SHA=head,
            CI_HEAD_SHA=head,
            LOCAL_CHANGE_DIRECTED="1",
        )

        self.assert_trailing_whitespace_failure(result, "candidate.txt")

    def test_invalid_revision_fails_closed_without_shell_interpolation(self) -> None:
        marker_path = self.repository.root / "unsafe-interpolation-marker"
        invalid_base = f"missing;touch {marker_path}"

        result = self.run_checks(
            CI_BASE_SHA=invalid_base,
            CI_HEAD_SHA=self.base,
            LOCAL_CHANGE_DIRECTED="1",
        )

        self.assertNotEqual(0, result.returncode)
        self.assertFalse(marker_path.exists())
        self.assertIn("could not resolve candidate Git range", result.stderr)

    def test_pr_range_rejects_secret_introduced_then_removed(self) -> None:
        secret = generated_secret()
        head = self.introduce_and_remove_secret(secret)

        result = self.run_checks(CI_BASE_SHA=self.base, CI_HEAD_SHA=head)

        self.assert_redacted_secret_failure(result, secret)

    def test_full_ci_rejects_untracked_working_tree_secret(self) -> None:
        secret = generated_secret()
        self.repository.write("untracked.env", f'api_key = "{secret}"\n')

        result = self.run_checks(FULL_SECRET_SCAN="1")

        self.assert_redacted_secret_failure(result, secret)

    def test_full_ci_rejects_secret_introduced_then_removed(self) -> None:
        secret = generated_secret()
        self.introduce_and_remove_secret(secret)

        result = self.run_checks(FULL_SECRET_SCAN="1")

        self.assert_redacted_secret_failure(result, secret)

    def test_local_change_directed_rejects_historical_branch_secret(self) -> None:
        secret = generated_secret()
        head = self.introduce_and_remove_secret(secret)

        result = self.run_checks(
            CI_BASE_SHA=self.base,
            CI_HEAD_SHA=head,
            LOCAL_CHANGE_DIRECTED="1",
        )

        self.assert_redacted_secret_failure(result, secret)

    def test_local_change_directed_rejects_untracked_working_tree_secret(self) -> None:
        secret = generated_secret()
        self.repository.write("untracked.env", f'api_key = "{secret}"\n')

        result = self.run_checks(
            CI_BASE_SHA=self.base,
            CI_HEAD_SHA=self.base,
            LOCAL_CHANGE_DIRECTED="1",
        )

        self.assert_redacted_secret_failure(result, secret)


if __name__ == "__main__":
    unittest.main()
