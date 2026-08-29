from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple, Sequence


APACHE_LICENSE_SIGNATURES = (
    "Apache License",
    "Version 2.0, January 2004",
)
NON_EXACT_TOOL_VERSIONS = {"latest", "lts", "stable", "system"}
EXACT_TOOL_VERSION_PATTERN = re.compile(
    r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$"
)
BOOTSTRAP_RECOVERY = "run mise run bootstrap"
TRUST_POLICY_EXCLUDES_OPTION = "trust_policy_excludes"


class TrustPolicyExceptionIdentity(NamedTuple):
    tool_name: str
    version: str
    backend: str
    excludes: tuple[str, ...]


APPROVED_TRUST_POLICY_EXCEPTION = TrustPolicyExceptionIdentity(
    tool_name="npm:markdownlint-cli2",
    version="0.23.2",
    backend="npm:markdownlint-cli2",
    excludes=("fastq@1.20.2",),
)


def relative_name(file_path: Path, repo_root: Path) -> str:
    return file_path.relative_to(repo_root).as_posix()


def validate_json(file_path: Path, repo_root: Path) -> list[str]:
    try:
        with file_path.open(encoding="utf-8") as source:
            json.load(source)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return [f"{relative_name(file_path, repo_root)}: invalid JSON"]
    return []


def validate_toml(file_path: Path, repo_root: Path) -> list[str]:
    try:
        with file_path.open("rb") as source:
            tomllib.load(source)
    except (tomllib.TOMLDecodeError, OSError):
        return [f"{relative_name(file_path, repo_root)}: invalid TOML"]
    return []


def validate_executable(file_path: Path, repo_root: Path) -> list[str]:
    try:
        mode = file_path.stat().st_mode
        with file_path.open("rb") as source:
            has_shebang = source.read(2) == b"#!"
    except OSError:
        return [f"{relative_name(file_path, repo_root)}: cannot inspect file mode"]

    has_executable_mode = bool(
        mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    )
    is_executable = os.access(file_path, os.X_OK)
    file_name = relative_name(file_path, repo_root)

    if (has_executable_mode or is_executable) and not has_shebang:
        return [f"{file_name}: executable file is missing a shebang"]
    if has_shebang and not (has_executable_mode and is_executable):
        return [f"{file_name}: shebang file is not executable"]
    return []


def discover_repository_files(repo_root: Path) -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "-C",
            os.fspath(repo_root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    relative_paths = [
        Path(os.fsdecode(raw_path))
        for raw_path in result.stdout.split(b"\0")
        if raw_path
    ]
    return sorted(
        file_path
        for relative_path in relative_paths
        if os.path.lexists(file_path := repo_root / relative_path)
    )


def validate_license(repo_root: Path) -> list[str]:
    try:
        license_text = (repo_root / "LICENSE").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["LICENSE: missing Apache-2.0 signature"]

    if all(signature in license_text for signature in APACHE_LICENSE_SIGNATURES):
        return []
    return ["LICENSE: missing Apache-2.0 signature"]


def exact_version(tool_value: object) -> str | None:
    requested: object = tool_value
    if isinstance(tool_value, dict):
        requested = tool_value.get("version")
    if not isinstance(requested, str):
        return None

    normalized = requested.strip()
    if not normalized or normalized.lower() in NON_EXACT_TOOL_VERSIONS:
        return None
    if any(character in normalized for character in "*<>=^~ "):
        return None
    if normalized.startswith(("path:", "prefix:", "ref:")):
        return None
    if not EXACT_TOOL_VERSION_PATTERN.fullmatch(normalized):
        return None
    return normalized


def validate_mise_trust_policy_excludes(
    tool_name: str,
    tool_value: object,
    lock_entries: object,
) -> list[str]:
    declares_exception = (
        isinstance(tool_value, dict)
        and TRUST_POLICY_EXCLUDES_OPTION in tool_value
    )
    if (
        tool_name != APPROVED_TRUST_POLICY_EXCEPTION.tool_name
        and not declares_exception
    ):
        return []

    expected_tool_value = {
        "version": APPROVED_TRUST_POLICY_EXCEPTION.version,
        TRUST_POLICY_EXCLUDES_OPTION: list(
            APPROVED_TRUST_POLICY_EXCEPTION.excludes
        ),
    }
    if (
        tool_name != APPROVED_TRUST_POLICY_EXCEPTION.tool_name
        or tool_value != expected_tool_value
    ):
        return [
            f".mise.toml: tool {tool_name} has an unapproved "
            f"{TRUST_POLICY_EXCLUDES_OPTION} value; {BOOTSTRAP_RECOVERY}"
        ]

    expected_lock_options = {
        TRUST_POLICY_EXCLUDES_OPTION: json.dumps(
            list(APPROVED_TRUST_POLICY_EXCEPTION.excludes)
        )
    }
    represented = (
        isinstance(lock_entries, list)
        and len(lock_entries) == 1
        and isinstance(lock_entries[0], dict)
        and lock_entries[0].get("version")
        == APPROVED_TRUST_POLICY_EXCEPTION.version
        and lock_entries[0].get("backend")
        == APPROVED_TRUST_POLICY_EXCEPTION.backend
        and lock_entries[0].get("options") == expected_lock_options
    )
    if represented:
        return []
    return [
        f"mise.lock: {TRUST_POLICY_EXCLUDES_OPTION} for "
        f"{tool_name}@{APPROVED_TRUST_POLICY_EXCEPTION.version} "
        f"is not represented; "
        f"{BOOTSTRAP_RECOVERY}"
    ]


def validate_mise_lock(repo_root: Path) -> list[str]:
    try:
        with (repo_root / ".mise.toml").open("rb") as source:
            mise_config = tomllib.load(source)
        with (repo_root / "mise.lock").open("rb") as source:
            mise_lock = tomllib.load(source)
    except (tomllib.TOMLDecodeError, OSError):
        return [
            f"mise.lock: cannot verify exact tool pins; {BOOTSTRAP_RECOVERY}"
        ]

    configured_tools = mise_config.get("tools", {})
    locked_tools = mise_lock.get("tools", {})
    if not isinstance(configured_tools, dict) or not isinstance(locked_tools, dict):
        return [
            f"mise.lock: cannot verify exact tool pins; {BOOTSTRAP_RECOVERY}"
        ]

    errors: list[str] = []
    for tool_name, tool_value in sorted(configured_tools.items()):
        requested_version = exact_version(tool_value)
        if requested_version is None:
            errors.append(
                f".mise.toml: tool {tool_name} must use an exact version; "
                f"{BOOTSTRAP_RECOVERY}"
            )
            continue

        lock_entries = locked_tools.get(tool_name, [])
        if not isinstance(lock_entries, list):
            lock_entries = []
        represented = any(
            isinstance(entry, dict)
            and entry.get("version") == requested_version
            and isinstance(entry.get("specifiers"), list)
            and requested_version in entry["specifiers"]
            for entry in lock_entries
        )
        if not represented:
            errors.append(
                f"mise.lock: exact pin {tool_name}@{requested_version} "
                f"is not represented; {BOOTSTRAP_RECOVERY}"
            )
        errors.extend(
            validate_mise_trust_policy_excludes(
                tool_name,
                tool_value,
                lock_entries,
            )
        )
    return errors


def repository_validation_errors(repo_root: Path) -> list[str]:
    errors: list[str] = []
    for file_path in discover_repository_files(repo_root):
        if file_path.suffix == ".json":
            errors.extend(validate_json(file_path, repo_root))
        elif file_path.suffix == ".toml":
            errors.extend(validate_toml(file_path, repo_root))
        errors.extend(validate_executable(file_path, repo_root))

    errors.extend(validate_license(repo_root))
    errors.extend(validate_mise_lock(repo_root))
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate repository structure")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    repo_root = arguments.root.resolve()

    try:
        errors = repository_validation_errors(repo_root)
    except subprocess.CalledProcessError:
        print("error: repository file discovery failed", file=sys.stderr)
        return 1

    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    print("Repository validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
