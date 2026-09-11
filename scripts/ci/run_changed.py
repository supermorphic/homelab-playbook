from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from collections.abc import Sequence

import classify
import molecule_plan


def commands_for(result: dict) -> list[list[str]]:
    commands = [["mise", "run", "validate:fast"]]
    if result["run_ansible"]:
        commands.append(["mise", "run", "validate:ansible"])
    selectors = dict.fromkeys(
        row["selector"] for row in result["molecule_plan"]["matrix"]["include"]
    )
    commands.extend(
        ["mise", "run", "test:molecule", "--", selector] for selector in selectors
    )
    if "semaphore/default" in selectors:
        commands.extend(
            ["mise", "run", "test:semaphore", "--", mode]
            for mode in ("compatibility", "fixture", "controller")
        )
    return commands


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
    resolved_base = resolved_head = None

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

    result["molecule_plan"] = molecule_plan.build_plan(result)
    result.update(
        {"base_sha": resolved_base, "head_sha": resolved_head, "include_worktree": True}
    )
    print("Molecule plan: " + classify.format_json(result["molecule_plan"]))
    print(
        f"Base SHA: {resolved_base or 'unresolved'}; "
        f"head SHA: {resolved_head or 'unresolved'}; includes worktree"
    )
    validation_environment = os.environ.copy()
    validation_environment.pop("HOMELAB_MOLECULE_PLATFORM", None)
    validation_environment.update(
        {
            "CI_BASE_SHA": resolved_base or arguments.base,
            "CI_HEAD_SHA": resolved_head or arguments.head,
            "LOCAL_CHANGE_DIRECTED": "1",
        }
    )
    for command in commands_for(result):
        if arguments.dry_run:
            print(f"Would run: {shlex.join(command)}")
            continue
        print(f"Running: {shlex.join(command)}", flush=True)
        completed = subprocess.run(command, check=False, env=validation_environment)
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
