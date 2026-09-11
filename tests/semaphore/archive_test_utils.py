from __future__ import annotations

import os
from pathlib import Path
import hashlib
import stat
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]


class ShellScriptTestCase(unittest.TestCase):
    script_path: Path

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.environment = os.environ.copy()
        self.environment["PATH"] = f"{self.bin_dir}:{self.environment['PATH']}"

    def executable(self, name: str, body: str) -> Path:
        path = self.bin_dir / name
        path.write_text("#!/bin/sh\nset -eu\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def raw_executable(self, name: str, source: str) -> Path:
        path = self.bin_dir / name
        path.write_text(source)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def run_script(self, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(self.script_path)],
            env={**self.environment, **environment},
            capture_output=True,
            text=True,
            check=False,
        )


def create_archive(root: Path, name: str, content: bytes = b"fixture dump") -> Path:
    archive = root / name
    archive.mkdir()
    (archive / "database.dump").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    (archive / "SHA256SUMS").write_text(f"{digest}  database.dump\n")
    return archive


def complete_text(content: bytes) -> str:
    return f"SHA256={hashlib.sha256(content).hexdigest()}\nBYTES={len(content)}\n"
