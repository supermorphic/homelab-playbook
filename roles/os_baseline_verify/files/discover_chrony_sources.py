#!/usr/bin/python3
"""Read Chrony source inputs recursively without changing target state.

Chrony configuration directives are case-insensitive. Nested includes are
bounded to ten levels and may repeat independently; only an active recursion
stack entry is a cycle.
"""

from __future__ import annotations

import glob
import json
import pathlib
import sys


MAX_NESTING = 10
SOURCE_DIRECTIVES = {"server", "pool", "peer"}
INPUT_SUFFIXES = {"confdir": ".conf", "sourcedir": ".sources"}


def _target_path(value: str, parent: pathlib.Path) -> pathlib.Path:
    candidate = pathlib.Path(value)
    return candidate if candidate.is_absolute() else parent / candidate


def _host_path(configured: pathlib.Path, root: pathlib.Path) -> pathlib.Path:
    if not configured.is_absolute():
        raise ValueError("chrony configuration path must be absolute")
    return root / configured.relative_to("/")


def discover(
    config_path: str,
    platform: str,
    root: pathlib.Path = pathlib.Path("/"),
) -> dict[str, object]:
    """Return active and disabled Chrony source state from a target root."""
    platform_name = platform.lower()
    if platform_name not in {"debian", "rocky"}:
        raise ValueError("chrony platform is unsupported")
    root_path = root.resolve()
    state: dict[str, object] = {
        "platform": platform_name,
        "sources": [],
        "disabled_sources": [],
        "active_inputs": [],
        "disabled_inputs": [],
        "markers": {"begin": 0, "end": 0},
    }
    stack: set[pathlib.Path] = set()

    def configured_path(path: pathlib.Path) -> pathlib.Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(root_path):
            raise ValueError("chrony configuration path escapes target root")
        return pathlib.Path("/") / resolved.relative_to(root_path)

    def add_source(
        source: str,
        directive: str,
        configured: pathlib.Path,
        resolved: pathlib.Path,
        disabled: bool,
    ) -> None:
        key = "disabled_sources" if disabled else "sources"
        state[key].append(  # type: ignore[index]
            {
                "source": source,
                "directive": directive,
                "configured_path": str(configured),
                "resolved_path": str(resolved),
            }
        )

    def add_input(
        kind: str,
        configured: list[pathlib.Path],
        resolved: list[pathlib.Path],
        disabled: bool,
    ) -> None:
        key = "disabled_inputs" if disabled else "active_inputs"
        state[key].append(  # type: ignore[index]
            {
                "kind": kind,
                "configured_paths": [str(path) for path in configured],
                "resolved_paths": [str(path) for path in resolved],
            }
        )

    def read(configured: pathlib.Path, depth: int, source_only: bool) -> None:
        if depth > MAX_NESTING:
            raise ValueError("chrony configuration nesting exceeds supported limit")
        host = _host_path(configured, root_path)
        resolved = host.resolve()
        if not resolved.is_file():
            raise ValueError("chrony configuration file is missing")
        if not resolved.is_relative_to(root_path):
            raise ValueError("chrony configuration path escapes target root")
        if resolved in stack:
            raise ValueError("chrony configuration includes a cycle")
        stack.add(resolved)
        try:
            target = configured_path(resolved)
            for raw in resolved.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line == "# BEGIN ANSIBLE MANAGED TRUSTED CHRONY SOURCES":
                    state["markers"]["begin"] += 1  # type: ignore[index]
                    continue
                if line == "# END ANSIBLE MANAGED TRUSTED CHRONY SOURCES":
                    state["markers"]["end"] += 1  # type: ignore[index]
                    continue
                disabled = line.startswith("# homelab-disabled: ")
                if disabled:
                    line = line.removeprefix("# homelab-disabled: ").strip()
                elif not line or line.startswith(("!", ";", "#", "%")):
                    continue
                fields = line.split()
                if not fields:
                    continue
                directive = fields[0].lower()
                if directive in SOURCE_DIRECTIVES:
                    if len(fields) < 2:
                        raise ValueError("chrony source directive is invalid")
                    add_source(fields[1], directive, target, target, disabled)
                    continue
                if source_only:
                    raise ValueError("chrony sourcedir contains a non-source directive")
                if directive == "include":
                    if len(fields) != 2:
                        raise ValueError("chrony include directive is invalid")
                    configured_input = _target_path(fields[1], target.parent)
                    matches = [
                        configured_path(pathlib.Path(value))
                        for value in sorted(glob.glob(str(_host_path(configured_input, root_path))))
                        if pathlib.Path(value).is_file()
                    ]
                    if not disabled and not matches:
                        raise ValueError("chrony include does not match a file")
                    add_input("include", [configured_input], matches, disabled)
                    if not disabled:
                        for child in matches:
                            read(child, depth + 1, False)
                    continue
                if directive in INPUT_SUFFIXES:
                    if not 2 <= len(fields) <= 11:
                        raise ValueError("chrony directory input count is invalid")
                    configured_inputs = [_target_path(value, target.parent) for value in fields[1:]]
                    selected: dict[str, pathlib.Path] = {}
                    for input_path in configured_inputs:
                        directory = _host_path(input_path, root_path)
                        if not directory.is_dir():
                            continue
                        for child in sorted(directory.glob(f"*{INPUT_SUFFIXES[directive]}")):
                            if child.is_file() and child.name not in selected:
                                selected[child.name] = child
                    children = [configured_path(path) for path in selected.values()]
                    add_input(directive, configured_inputs, children, disabled)
                    if not disabled:
                        for child in children:
                            read(child, depth + 1, directive == "sourcedir")
        finally:
            stack.remove(resolved)

    read(pathlib.Path(config_path), 0, False)
    return state


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: discover_chrony_sources.py PLATFORM CONFIG")
    print(json.dumps(discover(sys.argv[2], sys.argv[1]), sort_keys=True))
