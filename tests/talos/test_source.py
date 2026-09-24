from __future__ import annotations

import os
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys


FILES = Path(__file__).resolve().parents[2] / "roles/talos_lifecycle/files"
sys.path.insert(0, str(FILES))
import source as source_module  # noqa: E402
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
        (self.source / "kubernetes/apps/testing/echo/app").mkdir(parents=True)
        (self.source / "talos/talconfig.yaml").write_text(
            "endpoint: https://192.0.2.20:6443\n"
            "nodes:\n"
            "  - hostname: node-a\n    ipAddress: 192.0.2.10\n    controlPlane: true\n"
            "  - hostname: node-b\n    ipAddress: 192.0.2.11\n    controlPlane: true\n"
            "  - hostname: node-c\n    ipAddress: 192.0.2.12\n    controlPlane: true\n",
            encoding="utf-8",
        )
        (self.source / "kubernetes/apps/testing/echo/app/httproute.yaml").write_text(
            "spec:\n  hostnames:\n    - echo.example.test\n", encoding="utf-8"
        )
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.source), "remote", "add", "origin", ORIGIN], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "talos/talconfig.yaml", "kubernetes/apps/testing/echo/app/httproute.yaml"], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "fixture"], check=True)
        self.revision = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        cache = self.source / ".cache/recovery-helm"
        cache.mkdir(parents=True)
        charts: dict[str, dict[str, str]] = {}
        for logical in ("cilium", "cert-manager", "metallb", "envoy-gateway", "external-dns"):
            archive = cache / f"{logical}.tgz"
            archive.write_bytes(f"fixture-{logical}".encode())
            charts[logical] = {"file": archive.name, "chartName": "gateway-helm" if logical == "envoy-gateway" else logical,
                               "version": "1.0.0", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
        (cache / "manifest.json").write_text(json.dumps({"schemaVersion": 1, "sourceRevision": self.revision, "charts": charts}), encoding="utf-8")

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
        self.assertEqual(result["probe_dns_name"], "echo.example.test")
        self.assertEqual(result["probe_https_url"], "https://echo.example.test/")
        cache = Path(str(result["recovery_helm_cache"]))
        self.assertEqual({path.name for path in cache.iterdir()}, {"manifest.json", "cilium.tgz", "cert-manager.tgz", "metallb.tgz", "envoy-gateway.tgz", "external-dns.tgz"})
        self.assertFalse(cache.is_symlink())
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

    def test_missing_stale_and_symlinked_recovery_cache_are_rejected(self) -> None:
        cache = self.source / ".cache/recovery-helm"
        archive = cache / "cilium.tgz"
        archive.unlink()
        with self.assertRaises(SourceError):
            self.prepare(destination=Path(self.temporary.name) / "missing-cache")
        archive.write_bytes(b"fixture-cilium")
        manifest_path = cache / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["sourceRevision"] = "b" * 40
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(SourceError):
            self.prepare(destination=Path(self.temporary.name) / "stale-cache")
        manifest["sourceRevision"] = self.revision
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        archive.unlink()
        archive.symlink_to(cache / "cert-manager.tgz")
        with self.assertRaises(SourceError):
            self.prepare(destination=Path(self.temporary.name) / "symlink-cache")

    def test_hardlinked_or_changing_recovery_cache_is_rejected(self) -> None:
        cache = self.source / ".cache/recovery-helm"
        archive = cache / "cilium.tgz"
        original = archive.read_bytes()
        archive.unlink()
        os.link(cache / "cert-manager.tgz", archive)
        with self.assertRaises(SourceError):
            self.prepare(destination=Path(self.temporary.name) / "hardlink-cache")
        archive.unlink()
        archive.write_bytes(original)

        real_open = os.open
        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
            if Path(path).name == "manifest.json":
                archive.write_bytes(b"changed-during-copy")
            return descriptor
        with mock.patch.object(source_module.os, "open", side_effect=racing_open), self.assertRaises(SourceError):
            source_module._copy_recovery_cache(cache, Path(self.temporary.name) / "racing-cache", self.revision)

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
