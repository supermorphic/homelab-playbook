#!/usr/bin/env python3
"""Bounded metadata mirror test gateway; live mode requires attended arguments."""
import argparse
from pathlib import Path
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[2]
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['unit','fixture','live'])
    parser.add_argument('arguments',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    if args.mode=='live':
        from live import main as live_main
        return live_main(args.arguments)
    if args.arguments: parser.error('unexpected mode arguments')
    if args.mode=='fixture':
        return subprocess.run([sys.executable,'-m','unittest','discover','-s','tests/forgejo_metadata','-p','test_fixture.py','-v'],cwd=ROOT,timeout=180).returncode
    sys.path.insert(0,str(ROOT/'tests/forgejo_metadata'))
    import unittest
    suite=unittest.TestSuite()
    for path in sorted((ROOT/'tests/forgejo_metadata').glob('test_*.py')):
        if path.name!='test_fixture.py': suite.addTests(unittest.defaultTestLoader.discover(str(path.parent),pattern=path.name))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
if __name__ == '__main__': sys.exit(main())
