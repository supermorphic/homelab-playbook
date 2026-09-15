"""Compare proxy metadata acquisition with the pinned Ansible stat module."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "roles/reverse_proxy/library/proxy_metadata.py"
FIELDS = ("exists", "isdir", "isreg", "islnk", "uid", "gid", "mode",
          "nlink", "pw_name", "gr_name")


def invoke(command, arguments):
    result = subprocess.run(
        [sys.executable, *command],
        input=json.dumps({"ANSIBLE_MODULE_ARGS": arguments}),
        text=True, capture_output=True, check=False, timeout=20,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"Module returned invalid JSON (exit {result.returncode}):\n"
            f"stdout: {result.stdout[:2000]}\nstderr: {result.stderr[:2000]}"
        ) from error
    return result.returncode, payload


class ProxyMetadataTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), "The proxy metadata module is missing")
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def observe(self, paths, **arguments):
        return invoke([str(MODULE)], {"paths": list(map(str, paths)), **arguments})

    def oracle(self, path):
        return invoke(["-m", "ansible.modules.stat"],
                      {"path": str(path), "follow": False})

    def test_real_objects_match_stat_without_following_links(self):
        regular = self.root / "regular"
        regular.write_text("fixture\n")
        regular.chmod(0o640)
        directory = self.root / "directory"
        directory.mkdir(mode=0o750)
        sticky = self.root / "sticky"
        sticky.mkdir()
        sticky.chmod(0o1777)
        link = self.root / "link"
        link.symlink_to(regular)
        directory_link = self.root / "directory-link"
        directory_link.symlink_to(directory, target_is_directory=True)
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "absent")
        hardlink = self.root / "hardlink"
        os.link(regular, hardlink)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        sock_path = self.root / "socket"
        with socket.socket(socket.AF_UNIX) as sock:
            # A short relative path also works on macOS's short socket limit.
            previous = Path.cwd()
            try:
                os.chdir(self.root)
                sock.bind("socket")
            finally:
                os.chdir(previous)
            paths = [regular, directory, sticky, link, directory_link, dangling,
                     hardlink, fifo, sock_path, self.root / "missing",
                     self.root / "missing/child", regular]
            status, batch = self.observe(paths)
            self.assertEqual(0, status, batch)
            self.assertIs(batch["changed"], False)
            self.assertEqual(list(map(str, paths)), [r["item"] for r in batch["results"]])
            for path, result in zip(paths, batch["results"], strict=True):
                with self.subTest(path=path.name):
                    oracle_status, oracle = self.oracle(path)
                    self.assertEqual(0, oracle_status, oracle)
                    self.assertEqual(
                        {k: v for k, v in oracle["stat"].items() if k in FIELDS},
                        result["stat"],
                    )

    def test_other_lookup_errors_fail_instead_of_becoming_missing(self):
        regular = self.root / "regular"
        regular.touch()
        loop = self.root / "loop"
        loop.symlink_to(loop)
        for path in (regular / "child", loop / "child", self.root / ("x" * 300)):
            with self.subTest(path=str(path)):
                oracle_status, oracle = self.oracle(path)
                status, batch = self.observe([self.root / "missing", path])
                self.assertNotEqual(0, oracle_status)
                self.assertNotEqual(0, status)
                self.assertIs(batch["failed"], True)
                self.assertEqual(oracle["msg"], batch["msg"])
                self.assertEqual(str(path), batch.get("failed_path"))

    def test_check_mode_observes_fresh_metadata(self):
        path = self.root / "fresh"
        status, first = self.observe([path], _ansible_check_mode=True)
        self.assertEqual(0, status, first)
        self.assertEqual({"exists": False}, first["results"][0]["stat"])
        path.touch(mode=0o600)
        status, second = self.observe([path], _ansible_check_mode=True)
        self.assertEqual(0, status, second)
        self.assertIs(second["changed"], False)
        self.assertIs(second["results"][0]["stat"]["isreg"], True)
        self.assertEqual("0600", second["results"][0]["stat"]["mode"])

    def test_empty_batch_is_unchanged(self):
        status, result = self.observe([])
        self.assertEqual(0, status, result)
        self.assertEqual([], result["results"])
        self.assertIs(result["changed"], False)


if __name__ == "__main__":
    unittest.main()
