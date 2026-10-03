#!/usr/bin/env python3
"""Run bounded runner experiments independently of production inventory."""

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test Forgejo runners with disposable fixtures")
    parser.add_argument("mode", choices=("unit", "compatibility", "host-fixture", "job-fixture"))
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--workload", choices=("forgejo/default", "system_maintenance/default",
                        "system_maintenance/baseline", "semaphore/default", "reverse_proxy/default"),
                        help="Run a staged repository Molecule candidate in a real Forgejo job")
    parser.add_argument("--controller-failure", action="store_true",
                        help="Exercise forced controller failure during compatibility probes")
    args = parser.parse_args(argv)
    if args.workload and (args.mode != "compatibility" or args.controller_failure):
        parser.error("workloads require compatibility mode without controller failure")
    if args.controller_failure and args.mode != "compatibility":
        parser.error("controller failure requires compatibility mode")
    if args.mode == "unit":
        if args.fixture:
            parser.error("unit mode does not use a fixture descriptor")
        return subprocess.run([
            sys.executable, "-m", "unittest", "discover", "-s",
            "tests/forgejo_runner", "-p", "test_*.py", "-v",
        ], cwd=ROOT, check=False, timeout=900).returncode
    try:
        if args.mode == "host-fixture":
            raise RuntimeError("A separately supplied disposable Debian host fixture is required")
        from scripts.forgejo_runner.compatibility import run
        return run(args.fixture, registration=args.mode == "compatibility",
                   controller_failure=args.controller_failure, workload=args.workload)
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
