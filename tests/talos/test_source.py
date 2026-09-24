from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys


FILES = Path(__file__).resolve().parents[2] / "roles/talos_lifecycle/files"
sys.path.insert(0, str(FILES))
from source import SourceError, prepare_source  # noqa: E402


ORIGIN = "https://github.com/supermorphic/homelab-talos.git"


class SourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.destination = root / "prepared"
        (self.source / "talos").mkdir(parents=True)
        (self.source / "talos/talconfig.yaml").write_text(
            "endpoint: https://192.0.2.20:6443\n"
            "nodes:\n"
            "  - hostname: node-a\n    ipAddress: 192.0.2.10\n    controlPlane: true\n"
            "  - hostname: node-b\n    ipAddress: 192.0.2.11\n    controlPlane: true\n"
            "  - hostname: node-c\n    ipAddress: 192.0.2.12\n    controlPlane: true\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.source), "remote", "add", "origin", ORIGIN], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "talos/talconfig.yaml"], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "fixture"], check=True)
        self.revision = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()

    def prepare(self, **changes: object) -> dict[str, object]:
        values = {
            "source_dir": self.source,
            "revision": self.revision,
            "destination": self.destination,
            "trusted_origin": ORIGIN,
        }
        values.update(changes)
        return prepare_source(**values)  # type: ignore[arg-type]

    def test_prepares_private_exact_snapshot(self) -> None:
        result = self.prepare()
        self.assertEqual(result["revision"], self.revision)
        self.assertEqual(result["api_server"], "https://192.0.2.20:6443")
        self.assertEqual(result["nodes"], {"node-a": "192.0.2.10", "node-b": "192.0.2.11", "node-c": "192.0.2.12"})
        self.assertEqual(result["talos_endpoints"], ["192.0.2.10", "192.0.2.11", "192.0.2.12"])
        snapshot = Path(str(result["snapshot_dir"]))
        self.assertNotEqual(snapshot.resolve(), self.source.resolve())
        self.assertEqual(snapshot, Path(str(result["verification_dir"])))
        self.assertEqual(subprocess.check_output(["git", "-C", str(snapshot), "rev-parse", "HEAD"], text=True).strip(), self.revision)

    def test_wrong_origin_sha_dirty_and_path_escape_are_rejected(self) -> None:
        with self.assertRaises(SourceError):
            self.prepare(trusted_origin="https://example.invalid/wrong.git")
        with self.assertRaises(SourceError):
            self.prepare(revision="b" * 40)
        (self.source / "talos/talconfig.yaml").write_text("dirty", encoding="utf-8")
        with self.assertRaises(SourceError):
            self.prepare(destination=Path(self.temporary.name) / "dirty-prepared")

    def test_duplicate_nodes_addresses_and_non_control_plane_are_rejected(self) -> None:
        for replacement in (
            "  - hostname: node-a\n    ipAddress: 192.0.2.11\n    controlPlane: true\n",
            "  - hostname: node-c\n    ipAddress: 192.0.2.11\n    controlPlane: true\n",
            "  - hostname: node-c\n    ipAddress: 192.0.2.12\n    controlPlane: false\n",
        ):
            with self.subTest(replacement=replacement):
                original = (self.source / "talos/talconfig.yaml").read_text(encoding="utf-8")
                changed = original.replace("  - hostname: node-c\n    ipAddress: 192.0.2.12\n    controlPlane: true\n", replacement)
                (self.source / "talos/talconfig.yaml").write_text(changed, encoding="utf-8")
                subprocess.run(["git", "-C", str(self.source), "add", "talos/talconfig.yaml"], check=True)
                subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "case"], check=True)
                revision = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
                with self.assertRaises(SourceError):
                    self.prepare(revision=revision, destination=Path(self.temporary.name) / revision)
                subprocess.run(["git", "-C", str(self.source), "reset", "--hard", "-q", self.revision], check=True)


if __name__ == "__main__":
    unittest.main()
