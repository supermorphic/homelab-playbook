from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

IMPLEMENTED_DEPTHS = ("fast", "ansible", "molecule", "full")


def plan_errors(depth: str, payload: str) -> list[str]:
    from molecule_plan import SCENARIOS

    try:
        plan = json.loads(payload)
        mode = plan["mode"]
        rows = plan["matrix"]["include"]
        if mode not in {"none", "selective", "full"} or not isinstance(rows, list):
            raise ValueError("invalid plan structure")
        pairs = set()
        for row in rows:
            selector, platform = row["selector"], row["platform"]
            if selector not in SCENARIOS or platform not in {
                entry.name for entry in SCENARIOS[selector].platforms
            }:
                raise ValueError("unknown scenario or platform")
            if (selector, platform) in pairs:
                raise ValueError("duplicate scenario/platform row")
            reasons = row["reasons"]
            if (
                not isinstance(reasons, list)
                or not reasons
                or any(
                    not isinstance(reason, str) or not reason.strip()
                    for reason in reasons
                )
            ):
                raise ValueError("missing selection reasons")
            pairs.add((selector, platform))
        if depth in {"fast", "ansible"}:
            if mode != "none" or pairs:
                raise ValueError("unexpected Molecule selection")
        else:
            if mode == "none" or not pairs:
                raise ValueError("required Molecule selection is empty")
            selectors = SCENARIOS if mode == "full" else {pair[0] for pair in pairs}
            expected = {
                (selector, platform.name)
                for selector in selectors
                for platform in SCENARIOS[selector].platforms
            }
            if pairs != expected or (depth == "full" and mode != "full"):
                raise ValueError("incomplete scenario/platform coverage")
    except (ValueError, TypeError, KeyError) as error:
        return [f"invalid Molecule plan: {error}"]
    return []


def _result_error(job: str, result: str, expected: str) -> str:
    return f"{job} job result is '{result}', expected {expected}"


def reconcile(
    depth: str,
    classify_result: str,
    fast_result: str,
    ansible_result: str,
    molecule_result: str,
) -> list[str]:
    errors: list[str] = []
    if classify_result != "success":
        errors.append(_result_error("classify", classify_result, "'success'"))
    if fast_result != "success":
        errors.append(_result_error("fast", fast_result, "'success'"))

    if not depth:
        errors.append("validation depth is missing")
        return errors
    if depth not in IMPLEMENTED_DEPTHS:
        errors.append(f"validation depth '{depth}' is unknown")
        return errors

    if depth == "fast":
        if ansible_result not in {"success", "skipped"}:
            errors.append(
                _result_error("ansible", ansible_result, "'success' or 'skipped'")
            )
    elif ansible_result != "success":
        errors.append(_result_error("ansible", ansible_result, "'success'"))

    if depth in {"fast", "ansible"}:
        if molecule_result not in {"success", "skipped"}:
            errors.append(
                _result_error("molecule", molecule_result, "'success' or 'skipped'")
            )
    elif molecule_result != "success":
        errors.append(_result_error("molecule", molecule_result, "'success'"))

    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile GitHub validation job results"
    )
    parser.add_argument("--depth", required=True)
    parser.add_argument("--classify-result", required=True)
    parser.add_argument("--fast-result", required=True)
    parser.add_argument("--ansible-result", required=True)
    parser.add_argument("--molecule-result", required=True)
    parser.add_argument(
        "--molecule-plan", required=True, help="validate the selected matrix plan"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    errors = reconcile(
        arguments.depth,
        arguments.classify_result,
        arguments.fast_result,
        arguments.ansible_result,
        arguments.molecule_result,
    )
    errors.extend(plan_errors(arguments.depth, arguments.molecule_plan))
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
