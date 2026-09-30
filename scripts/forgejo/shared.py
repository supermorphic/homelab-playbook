"""Load the same host-side lifecycle implementation in controller experiments."""
import importlib
from pathlib import Path
import sys

from scripts.forgejo.runtime import ROOT

DIRECTORY = ROOT / 'roles/forgejo/files'
previous = sys.path[:]
try:
    sys.path.insert(0, str(DIRECTORY))
    for name in ('service', 'archive', 'backup', 'transfer'):
        module = importlib.import_module(name)
        if Path(module.__file__).resolve() != (DIRECTORY / (name + '.py')).resolve():
            raise RuntimeError('host helper module origin differs from this checkout')
        globals()[name] = module
finally:
    sys.path[:] = previous
