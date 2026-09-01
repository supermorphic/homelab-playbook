#!/usr/bin/python3
"""Read chrony source inputs recursively without changing target state."""

from __future__ import annotations

import glob
import json
import pathlib
import sys


def discover(config_path: str) -> dict[str, object]:
    state: dict[str, object] = {
        "sources": [], "disabled_sources": [], "active_inputs": [],
        "disabled_inputs": [], "markers": {"begin": 0, "end": 0},
    }
    visited: set[pathlib.Path] = set()

    def add_source(name: str, origin: str, disabled: bool) -> None:
        key = "disabled_sources" if disabled else "sources"
        state[key].append({"source": name, "origin": origin})  # type: ignore[index]

    def add_input(kind: str, disabled: bool) -> None:
        key = "disabled_inputs" if disabled else "active_inputs"
        state[key].append(kind)  # type: ignore[index]

    def expand(value: str, parent: pathlib.Path, kind: str) -> list[pathlib.Path]:
        target = pathlib.Path(value)
        if not target.is_absolute():
            target = parent / target
        if kind == "include":
            return [pathlib.Path(path) for path in sorted(glob.glob(str(target)))]
        suffix = "*.conf" if kind == "confdir" else "*.sources"
        return sorted(target.glob(suffix)) if target.is_dir() else []

    def read(path: pathlib.Path, origin: str) -> None:
        resolved = path.resolve()
        if resolved in visited:
            raise ValueError("chrony configuration includes a cycle")
        visited.add(resolved)
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
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) >= 2 and fields[0] in {"server", "pool"}:
                add_source(fields[1], origin, disabled)
            elif len(fields) == 2 and fields[0] in {"include", "confdir", "sourcedir"}:
                kind = fields[0]
                add_input(kind, disabled)
                if not disabled:
                    for child in expand(fields[1], resolved.parent, kind):
                        read(child, kind)

    read(pathlib.Path(config_path), "primary")
    return state


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: discover_chrony_sources.py CONFIG")
    print(json.dumps(discover(sys.argv[1]), sort_keys=True))
