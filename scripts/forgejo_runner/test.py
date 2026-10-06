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
    parser.add_argument("--host-fixture", type=Path,
                        help="Explicit disposable Debian target descriptor under .tmp")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Observe the disposable host without setup or containment acceptance")
    parser.add_argument('--resource-probe-only', action='store_true',
                        help='Bounded temporary host resource test; no runner or repository jobs')
    parser.add_argument('--worker-probe-only', action='store_true',
                        help='Offline temporary rootless worker test; no runner registration')
    parser.add_argument('--job-probe-only', action='store_true',
                        help='Offline real-job test in the temporary bounded NUC4 worker')
    parser.add_argument("--workload", choices=("forgejo/default", "system_maintenance/default",
                        "system_maintenance/baseline", "semaphore/default", "reverse_proxy/default"),
                        help="Run a staged repository Molecule candidate in a real Forgejo job")
    parser.add_argument("--controller-failure", action="store_true",
                        help="Exercise forced controller failure during compatibility probes")
    parser.add_argument('--workflow-contracts', action='store_true',
                        help='Exercise synthetic workflow outputs, artifacts and negative gates')
    args = parser.parse_args(argv)
    if (args.host_fixture or args.preflight_only or args.resource_probe_only or args.worker_probe_only or args.job_probe_only) and args.mode != "host-fixture":
        parser.error("host options require host-fixture mode")
    if args.resource_probe_only and (args.preflight_only or args.fixture):
        parser.error('resource probe cannot be combined with preflight or image fixtures')
    if args.worker_probe_only and (args.resource_probe_only or args.preflight_only or args.fixture):
        parser.error('worker probe cannot be combined with other host experiments')
    if args.job_probe_only and (args.resource_probe_only or args.preflight_only or args.worker_probe_only):
        parser.error('job probe cannot be combined with other host experiments')
    if args.workload and (args.mode != "compatibility" or args.controller_failure):
        parser.error("workloads require compatibility mode without controller failure")
    if args.controller_failure and args.mode != "compatibility":
        parser.error("controller failure requires compatibility mode")
    if args.workflow_contracts and (args.mode != 'compatibility' or args.workload or args.controller_failure):
        parser.error('workflow contracts require compatibility mode without other job probes')
    if args.mode == "unit":
        if args.fixture:
            parser.error("unit mode does not use a fixture descriptor")
        return subprocess.run([
            sys.executable, "-m", "unittest", "discover", "-s",
            "tests/forgejo_runner", "-p", "test_*.py", "-v",
        ], cwd=ROOT, check=False, timeout=900).returncode
    try:
        if args.mode == "host-fixture":
            from scripts.forgejo_runner.host_fixture import run
            if args.job_probe_only:
                return run(args.fixture, args.host_fixture, job_probe_only=True)
            if args.worker_probe_only:
                return run(args.fixture, args.host_fixture, worker_probe_only=True)
            if args.resource_probe_only:
                return run(args.fixture, args.host_fixture, resource_probe_only=True)
            return run(args.fixture, args.host_fixture, preflight_only=args.preflight_only)
        from scripts.forgejo_runner.compatibility import run
        return run(args.fixture, registration=args.mode == "compatibility",
                   controller_failure=args.controller_failure, workload=args.workload,
                   workflow_contracts=args.workflow_contracts)
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
