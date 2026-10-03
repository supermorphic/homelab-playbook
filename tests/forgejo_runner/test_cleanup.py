"""Fixture teardown must find children left by a failed controller."""

import json
import subprocess
import unittest

from scripts.forgejo_runner.fixture import RunnerRun


class CleanupTests(unittest.TestCase):
    def test_failed_controller_children_and_new_volumes_are_removed(self):
        experiment = RunnerRun(run_id="cleanup123")
        experiment.baseline_volumes = {"preexisting-volume"}
        controller = experiment.name("one-job")
        child = "engine-created-child"
        foreign = "unrelated-container"
        containers = {
            controller: {"Config": {"Labels": {experiment.label: experiment.run_id}}, "Mounts": []},
            child: {"Config": {"Labels": {experiment.label: experiment.run_id}}, "Mounts": [
                {"Type": "volume", "Name": "new-workspace"},
                {"Type": "volume", "Name": "preexisting-volume"}]},
            foreign: {"Config": {"Labels": {experiment.label: "different123"}}, "Mounts": []},
        }
        volumes = {name: {"Name": name, "CreatedAt": "2026-01-01T00:00:00Z", "Labels": {}}
                   for name in ("new-workspace", "preexisting-volume")}
        def command(argv, **kwargs):
            kind, action = argv[1:3]
            if action == "ls":
                self.assertNotIn(controller, containers, "controller must stop before child discovery")
                output = "\n".join(name for name, value in containers.items()
                    if value["Config"]["Labels"].get(experiment.label) == experiment.run_id)
                return subprocess.CompletedProcess(argv, 0, output, "")
            name = argv[-1]
            objects = containers if kind == "container" else volumes
            if action == "exists":
                return subprocess.CompletedProcess(argv, 0 if name in objects else 1, "", "")
            if action == "inspect":
                return subprocess.CompletedProcess(argv, 0, json.dumps([objects[name]]), "")
            if action == "rm":
                self.assertNotIn(name, (foreign, "preexisting-volume"))
                del objects[name]
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.fail("unexpected external operation")
        experiment.command = command
        experiment.resources.append(("container", controller))
        self.assertEqual([], experiment.cleanup())
        self.assertEqual({foreign}, set(containers))
        self.assertEqual({"preexisting-volume"}, set(volumes))


if __name__ == "__main__":
    unittest.main()
