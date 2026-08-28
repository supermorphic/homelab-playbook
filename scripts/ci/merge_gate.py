from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


IMPLEMENTED_DEPTHS = ("fast", "ansible", "full")


def _result_error(job: str, result: str, expected: str) -> str:
    return f"{job} job result is '{result}', expected {expected}"


def reconcile(
    depth: str,
    classify_result: str,
    fast_result: str,
    ansible_result: str,
) -> list[str]:
    errors: list[str] = []
    if classify_result != "success":
        errors.append(_result_error("classify", classify_result, "'success'"))

    if not depth:
        errors.append("validation depth is missing")
        return errors
    if depth == "molecule":
        errors.append("validation depth 'molecule' is not implemented")
        return errors
    if depth not in IMPLEMENTED_DEPTHS:
        errors.append(f"validation depth '{depth}' is unknown")
        return errors

    if fast_result != "success":
        errors.append(_result_error("fast", fast_result, "'success'"))

    if depth == "fast":
        if ansible_result not in {"success", "skipped"}:
            errors.append(
                _result_error("ansible", ansible_result, "'success' or 'skipped'")
            )
    elif ansible_result != "success":
        errors.append(_result_error("ansible", ansible_result, "'success'"))

    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile GitHub validation job results"
    )
    parser.add_argument("--depth", required=True)
    parser.add_argument("--classify-result", required=True)
    parser.add_argument("--fast-result", required=True)
    parser.add_argument("--ansible-result", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    errors = reconcile(
        arguments.depth,
        arguments.classify_result,
        arguments.fast_result,
        arguments.ansible_result,
    )
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
