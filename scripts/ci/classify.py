from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Sequence


DEPTH_ORDER = {"fast": 0, "ansible": 1, "molecule": 2, "full": 3}
EMITTED_DEPTHS = ("fast", "ansible", "full")
DIFF_DISCOVERY_OPTIONS = [
    "diff",
    "--name-status",
    "-z",
    "--find-renames",
    "--find-copies",
    "--find-copies-harder",
]

FULL_EXACT_PATHS = {
    "mise.lock",
    "pyproject.toml",
    "requirements.yml",
    "uv.lock",
}
SECURITY_VALIDATION_CONFIG_PATHS = {
    ".codespellrc",
    ".gitleaks.toml",
    ".gitleaksignore",
    ".markdownlint-cli2.yaml",
    ".pre-commit-config.yaml",
    ".yamllint",
}
ANSIBLE_CONFIG_PATHS = {
    ".ansible-lint",
    ".ansible-lint.yaml",
    ".ansible-lint.yml",
    "ansible-lint.yaml",
    "ansible-lint.yml",
    "ansible.cfg",
}
POLICY_PATHS = {
    "AGENTS.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
}


class GitDiscoveryError(RuntimeError):
    """Raised when Git cannot produce a complete changed-path set."""


def _has_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    return path.startswith(prefixes)


def _valid_relative_path(path: str) -> bool:
    if not path or "\0" in path or path.startswith(("/", "./")):
        return False
    return path != "." and ".." not in path.split("/")


def classify_path(path: str) -> tuple[str, str]:
    if not isinstance(path, str) or not _valid_relative_path(path):
        return "full", "invalid paths fail closed to full validation"

    if path == ".mise.toml":
        return "full", "Mise task configuration changes require full validation"
    if path in FULL_EXACT_PATHS:
        return "full", "toolchain or dependency changes require full validation"
    if path in SECURITY_VALIDATION_CONFIG_PATHS:
        return (
            "full",
            "security or validation configuration changes require full validation",
        )
    if _has_prefix(path, (".github/",)):
        return "full", "GitHub automation changes require full validation"
    if path in {"scripts/bootstrap.sh", "scripts/dependencies.py"}:
        return "full", "dependency automation changes require full validation"
    if _has_prefix(
        path,
        ("scripts/ci/", "tests/ci/", "tests/ansible/", "tests/toolchain/"),
    ):
        return "full", "validation implementation changes require full validation"

    if _has_prefix(path, ("inventory/", "playbooks/", "roles/", "overrides/")):
        return "ansible", "Ansible source changes require Ansible validation"
    if path in ANSIBLE_CONFIG_PATHS:
        return "ansible", "Ansible configuration changes require Ansible validation"

    if path in POLICY_PATHS or _has_prefix(path, ("docs/",)):
        return (
            "fast",
            "documentation, license, or policy changes require fast validation",
        )
    if path in {"run-playbook", "scripts/playbook.sh"} or _has_prefix(
        path, ("tests/operator/",)
    ):
        return "fast", "operator wrapper changes require fast validation"

    return "full", "unmapped paths fail closed to full validation"


def _result(
    depth: str,
    paths: list[str],
    reasons: dict[str, str],
) -> dict[str, object]:
    return {
        "depth": depth,
        "run_fast": True,
        "run_ansible": DEPTH_ORDER[depth] >= DEPTH_ORDER["ansible"],
        "run_molecule": False,
        "paths": paths,
        "reasons": reasons,
    }


def _failure_result(reason: str) -> dict[str, object]:
    return _result("full", [], {"<classifier>": reason})


def classify_paths(paths: list[str]) -> dict[str, object]:
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        return _failure_result("invalid changed-path input; full validation required")

    ordered_paths = sorted(set(paths))
    if not ordered_paths:
        return _failure_result(
            "no changed paths were discovered; full validation required"
        )

    depth = "fast"
    reasons: dict[str, str] = {}
    for path in ordered_paths:
        path_depth, reason = classify_path(path)
        reasons[path] = reason
        if DEPTH_ORDER[path_depth] > DEPTH_ORDER[depth]:
            depth = path_depth

    if depth not in EMITTED_DEPTHS:
        return _failure_result("invalid classifier result; full validation required")
    return _result(depth, ordered_paths, reasons)


def _run_git(arguments: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise GitDiscoveryError("Git command failed") from error
    return result.stdout


def _single_object_id(output: bytes) -> str:
    object_ids = output.splitlines()
    if (
        len(object_ids) != 1
        or len(object_ids[0]) < 40
        or any(character not in b"0123456789abcdefABCDEF" for character in object_ids[0])
    ):
        raise GitDiscoveryError("Git returned an invalid object identifier")
    return object_ids[0].decode("ascii")


def _resolve_commit(revision: str) -> str:
    if not revision or "\0" in revision:
        raise GitDiscoveryError("invalid Git revision")
    return _single_object_id(
        _run_git(
            [
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}^{{commit}}",
            ]
        )
    )


def _complete_nul_fields(output: bytes, stream_name: str) -> list[bytes]:
    if not output:
        return []
    if not output.endswith(b"\0"):
        raise GitDiscoveryError(f"Git returned unterminated {stream_name} output")

    fields = output[:-1].split(b"\0")
    if any(not field for field in fields):
        raise GitDiscoveryError(f"Git returned an empty {stream_name} record")
    return fields


def _status_path_count(status: bytes) -> int:
    if status in {b"A", b"D", b"M", b"T", b"U", b"X", b"B"}:
        return 1
    if status[:1] not in {b"C", b"R"}:
        raise GitDiscoveryError("Git returned an invalid name-status token")

    score = status[1:]
    valid_score = len(score) == 3 and score.isdigit() and int(score) <= 100
    if not valid_score:
        raise GitDiscoveryError("Git returned an invalid copy/rename score")
    return 2


def _parse_name_status(output: bytes) -> list[str]:
    fields = _complete_nul_fields(output, "name-status")

    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        path_count = _status_path_count(status)
        if index + path_count > len(fields):
            raise GitDiscoveryError("Git returned incomplete name-status output")
        for encoded_path in fields[index : index + path_count]:
            paths.append(os.fsdecode(encoded_path))
        index += path_count

    return paths


def _parse_nul_paths(output: bytes) -> list[str]:
    return [
        os.fsdecode(encoded_path)
        for encoded_path in _complete_nul_fields(output, "path")
    ]


def discover_changes(base: str, head: str, include_worktree: bool) -> list[str]:
    resolved_base = _resolve_commit(base)
    resolved_head = _resolve_commit(head)
    merge_base = _single_object_id(
        _run_git(["merge-base", resolved_base, resolved_head])
    )

    paths = set(
        _parse_name_status(
            _run_git(
                [
                    *DIFF_DISCOVERY_OPTIONS,
                    merge_base,
                    resolved_head,
                    "--",
                ]
            )
        )
    )
    if include_worktree:
        paths.update(
            _parse_name_status(
                _run_git(
                    [
                        *DIFF_DISCOVERY_OPTIONS,
                        "--cached",
                        "--",
                    ]
                )
            )
        )
        paths.update(
            _parse_name_status(
                _run_git(
                    [*DIFF_DISCOVERY_OPTIONS, "--"]
                )
            )
        )
        untracked_output = _run_git(
            ["ls-files", "-z", "--others", "--exclude-standard", "--"]
        )
        paths.update(_parse_nul_paths(untracked_output))

    return sorted(paths)


def force_depth(
    result: dict[str, object], requested_depth: str | None
) -> dict[str, object]:
    if requested_depth is None:
        return result
    if requested_depth not in EMITTED_DEPTHS:
        raise ValueError(f"unsupported forced depth: {requested_depth}")

    classified_depth = result.get("depth")
    if classified_depth not in EMITTED_DEPTHS:
        return _failure_result("invalid classifier result; full validation required")
    if DEPTH_ORDER[requested_depth] < DEPTH_ORDER[classified_depth]:
        raise ValueError(
            f"cannot de-escalate from {classified_depth} to {requested_depth}"
        )
    if requested_depth == classified_depth:
        return result

    paths = result.get("paths")
    reasons = result.get("reasons")
    if not isinstance(paths, list) or not isinstance(reasons, dict):
        return _failure_result("invalid classifier result; full validation required")
    return _result(requested_depth, list(paths), dict(reasons))


def format_json(result: dict[str, object]) -> str:
    return json.dumps(result, ensure_ascii=True, separators=(",", ":"))


def format_text(result: dict[str, object]) -> str:
    lines = [
        f"depth: {result['depth']}",
        f"run_fast: {str(result['run_fast']).lower()}",
        f"run_ansible: {str(result['run_ansible']).lower()}",
        f"run_molecule: {str(result['run_molecule']).lower()}",
        "paths:",
    ]
    paths = result["paths"]
    reasons = result["reasons"]
    if not isinstance(paths, list) or not isinstance(reasons, dict):
        return format_text(
            _failure_result("invalid classifier result; full validation required")
        )
    reason_paths = paths if paths else sorted(reasons)
    for path in reason_paths:
        lines.append(f"  {json.dumps(path, ensure_ascii=True)}: {reasons[path]}")
    return "\n".join(lines)


def format_github(result: dict[str, object]) -> str:
    return "\n".join(
        [
            f"depth={result['depth']}",
            f"run_fast={str(result['run_fast']).lower()}",
            f"run_ansible={str(result['run_ansible']).lower()}",
            f"run_molecule={str(result['run_molecule']).lower()}",
            "paths=" + json.dumps(result["paths"], ensure_ascii=True, separators=(",", ":")),
            "reasons="
            + json.dumps(result["reasons"], ensure_ascii=True, separators=(",", ":")),
        ]
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify changed paths into an offline validation depth"
    )
    parser.add_argument("--base", default="origin/main", metavar="REF")
    parser.add_argument("--head", default="HEAD", metavar="REF")
    parser.add_argument("--include-worktree", action="store_true")
    parser.add_argument("--force-depth", choices=EMITTED_DEPTHS)
    parser.add_argument("--format", choices=("text", "json", "github"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        paths = discover_changes(
            arguments.base, arguments.head, arguments.include_worktree
        )
        result = classify_paths(paths)
        result = force_depth(result, arguments.force_depth)
    except GitDiscoveryError:
        result = _failure_result(
            "Git change discovery failed; full validation required"
        )
        try:
            result = force_depth(result, arguments.force_depth)
        except ValueError as error:
            parser.error(str(error))
    except ValueError as error:
        parser.error(str(error))

    formatter = {
        "text": format_text,
        "json": format_json,
        "github": format_github,
    }[arguments.format]
    print(formatter(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
