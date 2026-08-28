from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


class CandidateCheckError(RuntimeError):
    """Raised when candidate Git state cannot be validated safely."""


def _run_captured(command: list[str], repo_root: Path) -> bytes:
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise CandidateCheckError from error
    return result.stdout


def _object_id(output: bytes) -> str:
    object_ids = output.splitlines()
    if (
        len(object_ids) != 1
        or len(object_ids[0]) < 40
        or any(
            character not in b"0123456789abcdefABCDEF"
            for character in object_ids[0]
        )
    ):
        raise CandidateCheckError
    return object_ids[0].decode("ascii")


def _resolve_commit(repo_root: Path, revision: str) -> str:
    if not revision or "\0" in revision:
        raise CandidateCheckError
    return _object_id(
        _run_captured(
            [
                "git",
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}^{{commit}}",
            ],
            repo_root,
        )
    )


def _candidate_range(repo_root: Path) -> tuple[str, str] | None:
    base_revision = os.environ.get("CI_BASE_SHA", "")
    head_revision = os.environ.get("CI_HEAD_SHA", "")
    if not base_revision and not head_revision:
        return None
    if not base_revision or not head_revision:
        raise CandidateCheckError

    resolved_base = _resolve_commit(repo_root, base_revision)
    resolved_head = _resolve_commit(repo_root, head_revision)
    merge_base = _object_id(
        _run_captured(
            ["git", "merge-base", resolved_base, resolved_head], repo_root
        )
    )
    return merge_base, resolved_head


def _run(command: list[str], repo_root: Path) -> int:
    try:
        return subprocess.run(command, cwd=repo_root, check=False).returncode
    except OSError:
        return 127


def check_whitespace(
    repo_root: Path,
    candidate_range: tuple[str, str] | None,
    include_worktree: bool,
) -> int:
    commands: list[list[str]] = []
    if candidate_range is not None:
        merge_base, resolved_head = candidate_range
        commands.append(
            ["git", "diff", "--check", merge_base, resolved_head, "--"]
        )
    if include_worktree or candidate_range is None:
        commands.extend(
            [
                ["git", "diff", "--cached", "--check", "--"],
                ["git", "diff", "--check", "--"],
            ]
        )

    for command in commands:
        if (returncode := _run(command, repo_root)) != 0:
            return returncode
    return 0


def check_secrets(
    repo_root: Path,
    candidate_range: tuple[str, str] | None,
    include_worktree: bool,
    full_history: bool,
) -> int:
    commands: list[list[str]] = []
    if full_history:
        commands.append(
            ["gitleaks", "git", "--redact", "--log-opts=--all", "."]
        )
    elif candidate_range is not None:
        merge_base, resolved_head = candidate_range
        commands.append(
            [
                "gitleaks",
                "git",
                "--redact",
                f"--log-opts={merge_base}..{resolved_head}",
                ".",
            ]
        )

    if include_worktree or full_history or candidate_range is None:
        commands.append(["gitleaks", "dir", "--redact", "."])

    for command in commands:
        if (returncode := _run(command, repo_root)) != 0:
            return returncode
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate candidate whitespace and secret coverage"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    arguments = parser.parse_args(argv)
    repo_root = arguments.repo_root.resolve()
    include_worktree = os.environ.get("LOCAL_CHANGE_DIRECTED") == "1"
    full_history = os.environ.get("FULL_SECRET_SCAN") == "1"

    try:
        candidate_range = _candidate_range(repo_root)
    except CandidateCheckError:
        print("error: could not resolve candidate Git range", file=sys.stderr)
        return 1

    whitespace_status = check_whitespace(
        repo_root, candidate_range, include_worktree
    )
    if whitespace_status != 0:
        return whitespace_status
    return check_secrets(
        repo_root, candidate_range, include_worktree, full_history
    )


if __name__ == "__main__":
    raise SystemExit(main())
