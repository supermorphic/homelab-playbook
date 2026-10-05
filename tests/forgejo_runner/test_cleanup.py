"""Fixture teardown must find children left by a failed controller."""

import json
import subprocess
import unittest

from scripts.forgejo_runner.fixture import RunnerRun


class CleanupTests(unittest.TestCase):
    def test_interrupted_molecule_child_is_removed_but_foreign_labels_are_preserved(self):
        from scripts.molecule import SCENARIOS, ownership_labels
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                experiment = RunnerRun(run_id="molecule123")
                experiment.baseline_volumes = set()
                scenario = SCENARIOS["forgejo/default"]
                platform = scenario.platforms[0]
                experiment.molecule_scenario = scenario
                labels = ownership_labels(platform, scenario)
                if foreign:
                    labels["io.supermorphic.homelab-playbook.repository"] = "other"
                containers = {platform.container: {"Id": "123owned", "Config": {"Labels": labels}}}
                def command(argv, **kwargs):
                    action = argv[2]
                    name = argv[-1]
                    if action == "ls":
                        return subprocess.CompletedProcess(argv, 0, "", "")
                    if action == "exists":
                        return subprocess.CompletedProcess(argv, 0 if name in containers else 1, "", "")
                    if action == "inspect":
                        return subprocess.CompletedProcess(argv, 0, json.dumps([containers[name]]), "")
                    if action == "rm":
                        self.assertFalse(foreign, "Must preserve conflicting ownership")
                        self.assertEqual("123owned", name, "Remove by inspected immutable container ID")
                        containers.clear()
                        return subprocess.CompletedProcess(argv, 0, "", "")
                    self.fail("unexpected external operation")
                experiment.command = command
                errors = experiment.cleanup()
                if foreign:
                    self.assertTrue(errors)
                    self.assertIn(platform.container, containers)
                else:
                    self.assertEqual([], errors)
                    self.assertEqual({}, containers)

    def test_failed_controller_children_and_new_volumes_are_removed(self):
        for suffix in ('one-job', 'workflow-1', 'workflow-2'):
            with self.subTest(controller=suffix):
                self.check_controller_cleanup(suffix)

    def test_failed_controller_start_is_retained_for_ordered_cleanup(self):
        self.check_controller_cleanup('workflow-3', failed_start=True)

    def check_controller_cleanup(self, suffix, *, failed_start=False):
        experiment = RunnerRun(run_id="cleanup123")
        experiment.baseline_volumes = {"preexisting-volume"}
        controller = experiment.name(suffix)
        experiment.controllers = set() if failed_start else {controller}
        child = "engine-created-child"
        foreign = "unrelated-container"
        containers = {
            controller: {"Config": {"Labels": {experiment.label: experiment.run_id}}, "Mounts": []},
            child: {"Config": {"Labels": {experiment.label: experiment.run_id}}, "Mounts": [
                {"Type": "volume", "Name": "new-workspace"},
                {"Type": "volume", "Name": "preexisting-volume"}]},
            foreign: {"Config": {"Labels": {experiment.label: "different123"}}, "Mounts": []},
        }
        controller_document = containers[controller]
        if failed_start:
            del containers[controller]
        volumes = {name: {"Name": name, "CreatedAt": "2026-01-01T00:00:00Z", "Labels": {}}
                   for name in ("new-workspace", "preexisting-volume")}
        def command(argv, **kwargs):
            if argv[1] == 'run':
                containers[controller] = controller_document
                raise RuntimeError('Synthetic engine failed after controller creation')
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
        if failed_start:
            self.assertTrue(callable(getattr(experiment, 'create_controller', None)),
                            'Controller ownership is not recorded before engine creation')
            with self.assertRaises(RuntimeError):
                experiment.create_controller(suffix, [])
        else:
            experiment.resources.append(("container", controller))
        self.assertEqual([], experiment.cleanup())
        self.assertEqual({foreign}, set(containers))
        self.assertEqual({"preexisting-volume"}, set(volumes))


if __name__ == "__main__":
    unittest.main()
