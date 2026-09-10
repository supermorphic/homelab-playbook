"""Deterministic selection within the Molecule validation tier."""

from __future__ import annotations

import json
import html
import sys
from pathlib import Path

import classify

# The runner registry is the authority for the complete suite, including when
# the impact map is missing or invalid. Importing it does not invoke Podman.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.molecule import SCENARIOS

MAP_PATH = Path(__file__).with_name("molecule-impact.json")


def _load_rules(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"rules"}:
        raise ValueError("invalid impact map")
    rules = document["rules"]
    if not isinstance(rules, list) or not rules:
        raise ValueError("empty impact map")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {
            "prefixes",
            "selectors",
            "reason",
        }:
            raise ValueError("invalid impact rule")
        prefixes, selectors = rule["prefixes"], rule["selectors"]
        if (
            not isinstance(prefixes, list)
            or not prefixes
            or any(
                not isinstance(prefix, str)
                or not classify._valid_relative_path(prefix)
                or not prefix.endswith("/")
                for prefix in prefixes
            )
        ):
            raise ValueError("invalid impact prefixes")
        if selectors != "all" and (
            not isinstance(selectors, list)
            or not selectors
            or any(
                not isinstance(selector, str) or selector not in SCENARIOS
                for selector in selectors
            )
        ):
            raise ValueError("unknown impact selector")
        if not isinstance(rule["reason"], str) or not rule["reason"].strip():
            raise ValueError("missing impact reason")
    return rules


def _plan(mode: str, selected: dict[str, set[str]]) -> dict:
    return {
        "mode": mode,
        "matrix": {
            "include": [
                {
                    "selector": selector,
                    "platform": platform.name,
                    "reasons": sorted(selected[selector]),
                }
                for selector, scenario in SCENARIOS.items()
                if selector in selected
                for platform in scenario.platforms
            ]
        },
    }


def _full(reasons: list[str]) -> dict:
    return _plan("full", {selector: set(reasons) for selector in SCENARIOS})


def build_plan(result: dict, *, map_path: Path = MAP_PATH) -> dict:
    depth = result.get("depth")
    if depth in {"fast", "ansible"}:
        return _plan("none", {})
    if depth != "molecule":
        reasons = result.get("reasons", {})
        return _full(
            ["full validation depth requires the complete suite"]
            + [f"{path}: {reason}" for path, reason in sorted(reasons.items())]
        )
    try:
        rules = _load_rules(map_path)
    except (OSError, ValueError, TypeError):
        return _full(["missing or invalid impact map; complete suite required"])
    selected: dict[str, set[str]] = {}
    full_reasons = []
    for path in result["paths"]:
        if classify.classify_path(path)[0] in {"fast", "ansible"}:
            continue
        matches = [
            (len(prefix), rule)
            for rule in rules
            for prefix in rule["prefixes"]
            if path.startswith(prefix)
        ]
        if "/molecule/" in path:
            # A role rule does not declare ownership of a newly added scenario.
            # Require a scenario directory rule even when the role is mapped.
            parts = path.split("/")
            scenario_prefix = "/".join(parts[:4]) + "/"
            matches = [
                (length, rule)
                for length, rule in matches
                if len(parts) >= 5
                and parts[2] == "molecule"
                and length >= len(scenario_prefix)
            ]
        if not matches:
            full_reasons.append(f"{path}: unmapped Molecule impact")
            continue
        longest = max(length for length, _ in matches)
        for length, rule in matches:
            if length != longest:
                continue
            reason = f"{path}: {rule['reason']}"
            if rule["selectors"] == "all":
                full_reasons.append(reason)
            else:
                for selector in rule["selectors"]:
                    selected.setdefault(selector, set()).add(reason)
    if full_reasons:
        return _full(full_reasons)
    if not selected:
        return _full(["no mapped Molecule impact; complete suite required"])
    return _plan("selective", selected)


def format_summary(result: dict) -> str:
    # Escape arbitrary Git path text so it cannot inject summary markup.
    payload = {
        key: result[key]
        for key in ("base_sha", "head_sha", "include_worktree", "molecule_plan")
    }
    return (
        "### Molecule validation plan\n\n<pre>"
        + html.escape(json.dumps(payload, ensure_ascii=True, indent=2))
        + "</pre>\n\n"
    )
