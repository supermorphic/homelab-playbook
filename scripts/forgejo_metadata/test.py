#!/usr/bin/env python3
"""Bounded offline metadata mirror test gateway."""
import argparse
from pathlib import Path
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[2]
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['unit'])
    parser.parse_args()
    return subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s',
        'tests/forgejo_metadata', '-p', 'test_*.py', '-v'], cwd=ROOT, timeout=900).returncode
if __name__ == '__main__': sys.exit(main())
