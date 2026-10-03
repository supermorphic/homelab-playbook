"""Runtime observations must prove the current worker prerequisites."""

import importlib.util
from pathlib import Path
import unittest
import tempfile
import json

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts/forgejo_runner/fixture.py"


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), "runner fixture validation is missing")
        spec = importlib.util.spec_from_file_location("runner_fixture", MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def info(self, *, rootless=True, cgroup="v2"):
        return {"host": {"security": {"rootless": rootless},
                         "cgroupVersion": cgroup, "arch": "aarch64"},
                "store": {"runRoot": "/run/user/1234/containers"}}

    def test_worker_socket_is_derived_from_the_observed_rootless_runtime(self):
        result = self.module.runtime_info(self.info())
        self.assertEqual("/run/user/1234/podman/podman.sock", result["socket"])
        self.assertEqual("arm64", result["architecture"])

    def test_rootful_runtime_is_rejected(self):
        with self.assertRaises(ValueError):
            self.module.runtime_info(self.info(rootless=False))

    def test_missing_or_old_cgroups_are_rejected(self):
        for value in (None, "v1", "unknown"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.module.runtime_info(self.info(cgroup=value))

    def test_unexpected_runroot_is_rejected(self):
        for path in ("/run/containers/storage", "/tmp/runtime", "/run/user/x/containers"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                info = self.info()
                info["store"]["runRoot"] = path
                self.module.runtime_info(info)

    def test_candidate_rejects_mutable_images_before_creating_resources(self):
        self.assertTrue(callable(getattr(self.module, "load_candidate", None)), "candidate validation is missing")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            path.write_text(json.dumps({"probe_images": {"arm64": "quay.io/podman/stable:latest"}}))
            with self.assertRaises(ValueError):
                self.module.load_candidate(path, "arm64")

    def test_candidate_requires_an_image_for_the_observed_architecture(self):
        self.assertTrue(callable(getattr(self.module, "load_candidate", None)), "candidate validation is missing")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            path.write_text(json.dumps({"probe_images": {}}))
            with self.assertRaises(ValueError):
                self.module.load_candidate(path, "amd64")

    def test_cleanup_failure_preserves_the_primary_failure(self):
        self.assertTrue(callable(getattr(self.module, "cleaned_up", None)), "cleanup guard is missing")
        class FailingCleanup:
            def cleanup(self):
                return ["synthetic owned resource could not be removed"]
        with self.assertRaises(RuntimeError) as caught:
            with self.module.cleaned_up(FailingCleanup()):
                raise RuntimeError("primary probe failed")
        self.assertIn("primary probe failed", str(caught.exception))
        self.assertIn("cleanup failed", str(caught.exception))

    def test_cleanup_preserves_a_resource_with_changed_ownership(self):
        experiment = self.module.RunnerRun(run_id="owned123")
        # The external command boundary returns a real inspect response; cleanup
        # must inspect ownership before it issues a remove request.
        requests = []
        def command(argv, **kwargs):
            import subprocess
            requests.append(argv)
            if argv[1:3] == ["container", "exists"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[1:3] == ["container", "inspect"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps([
                    {"Config": {"Labels": {experiment.label: "foreign123"}}}]), "")
            self.fail("cleanup attempted to remove a foreign resource")
        experiment.command = command
        experiment.resources.append(("container", "fixture"))
        self.assertTrue(experiment.cleanup())
        self.assertEqual(2, len(requests))


if __name__ == "__main__":
    unittest.main()
