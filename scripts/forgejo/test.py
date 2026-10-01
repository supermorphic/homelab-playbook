#!/usr/bin/env python3
"""Canonical dispatcher for bounded Forgejo experiments."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Test Forgejo with owned fixtures")
    parser.add_argument("mode", choices=("unit", "compatibility", "fixture", "restore"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.arguments and args.mode != "restore":
        parser.error("unexpected mode arguments")
    if args.mode == "unit":
        return subprocess.run([
            sys.executable, "-m", "unittest", "discover", "-s", "tests/forgejo",
            "-p", "test_*.py", "-v",
        ], cwd=ROOT, check=False, timeout=900).returncode
    try:
        module = importlib.import_module(f"scripts.forgejo.{args.mode}")
        return module.main(args.arguments) if args.mode == "restore" else module.run()
    except ModuleNotFoundError:
        print("Requested experiment is not installed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
