from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from collections.abc import Sequence

import classify


COMMANDS = {
    "fast": [["mise", "run", "validate:fast"]],
    "ansible": [
        ["mise", "run", "validate:fast"],
        ["mise", "run", "validate:ansible"],
    ],
    "molecule": [
        ["mise", "run", "validate:fast"],
        ["mise", "run", "validate:ansible"],
        [
            "mise",
            "run",
            "test:molecule",
            "--",
            "system_maintenance/default",
        ],
    ],
    "full": [
        ["mise", "run", "validate:fast"],
        ["mise", "run", "validate:ansible"],
        [
            "mise",
            "run",
            "test:molecule",
            "--",
            "system_maintenance/default",
        ],
    ],
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify local changes and run required offline validation"
    )
    parser.add_argument("--base", default="origin/main", metavar="REF")
    parser.add_argument("--head", default="HEAD", metavar="REF")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-depth", choices=classify.EMITTED_DEPTHS)
    return parser


def _print_reasons(result: dict[str, object]) -> None:
    paths = result["paths"]
    reasons = result["reasons"]
    if not isinstance(paths, list) or not isinstance(reasons, dict):
        raise ValueError("invalid classifier result")
    reason_paths = paths if paths else sorted(reasons)
    for path in reason_paths:
        print(f"{json.dumps(path, ensure_ascii=True)}: {reasons[path]}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    resolved_base = arguments.base
    resolved_head = arguments.head

    try:
        resolved_base, resolved_head = classify.resolve_commits(
            arguments.base, arguments.head
        )
        paths = classify.discover_changes(resolved_base, resolved_head, True)
        classified_result = classify.classify_paths(paths)
        result = classify.force_depth(classified_result, arguments.force_depth)
    except classify.GitDiscoveryError:
        classified_result = classify.classify_paths([])
        classified_result["reasons"] = {
            "<classifier>": "Git change discovery failed; full validation required"
        }
        try:
            result = classify.force_depth(classified_result, arguments.force_depth)
        except ValueError as error:
            parser.error(str(error))
    except ValueError as error:
        parser.error(str(error))

    classified_depth = classified_result["depth"]
    selected_depth = result["depth"]
    if not isinstance(classified_depth, str) or not isinstance(selected_depth, str):
        parser.error("invalid classifier depth")

    print(f"Selected validation depth: {selected_depth}")
    if selected_depth != classified_depth:
        print(f"Escalated validation depth: {classified_depth} -> {selected_depth}")
    _print_reasons(classified_result)

    validation_environment = os.environ.copy()
    validation_environment.update(
        {
            "CI_BASE_SHA": resolved_base,
            "CI_HEAD_SHA": resolved_head,
            "LOCAL_CHANGE_DIRECTED": "1",
        }
    )
    for command in COMMANDS[selected_depth]:
        if arguments.dry_run:
            print(f"Would run: {shlex.join(command)}")
            continue
        print(f"Running: {shlex.join(command)}", flush=True)
        completed = subprocess.run(
            command, check=False, env=validation_environment
        )
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
