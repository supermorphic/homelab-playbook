from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "ci"))
import classify


class MoleculePlanTests(unittest.TestCase):
    def plan(self, paths, depth=None):
        result = classify.force_depth(classify.classify_paths(paths), depth)
        # The public classifier owns depth; the planner adds scenario selection.
        import molecule_plan

        return molecule_plan.build_plan(result)

    def test_selection_table(self):
        maintenance = {"system_maintenance/default", "system_maintenance/baseline"}
        consumers = {"system_maintenance/baseline", "reverse_proxy/default"}
        all_scenarios = maintenance | consumers
        cases = [
            (
                ["roles/system_maintenance/molecule/default/verify.yml"],
                {"system_maintenance/default"},
                "selective",
            ),
            (
                ["roles/host_identity/tasks/main.yml"],
                {"system_maintenance/baseline"},
                "selective",
            ),
            (["roles/security_baseline/tasks/firewall.yml"], consumers, "selective"),
            (["roles/os_baseline_verify/tasks/firewall.yml"], consumers, "selective"),
            (
                ["roles/reverse_proxy/molecule/future/verify.yml"],
                all_scenarios,
                "full",
            ),
            (
                ["roles/system_maintenance/molecule/future/verify.yml"],
                all_scenarios,
                "full",
            ),
            (
                ["roles/reverse_proxy/files/README.md"],
                consumers,
                "selective",
            ),
            (["roles/system_maintenance/tasks/main.yml"], maintenance, "selective"),
            (["roles/reverse_proxy/tasks/main.yml"], consumers, "selective"),
            (["playbooks/reverse-proxy/provision.yml"], consumers, "selective"),
            (
                ["roles/reverse_proxy/molecule/default/verify.yml"],
                {"reverse_proxy/default"},
                "selective",
            ),
            (
                ["roles/tls_automation/files/tls_runtime/runtime.py"],
                consumers,
                "selective",
            ),
            (
                ["roles/podman_foundation/tasks/main.yml"],
                {"system_maintenance/baseline"},
                "selective",
            ),
            (
                ["roles/system_maintenance/molecule/default/create.yml"],
                all_scenarios,
                "full",
            ),
            (
                ["roles/system_maintenance/molecule/default/molecule.yml"],
                all_scenarios,
                "full",
            ),
            (["roles/new_role/molecule/default/verify.yml"], all_scenarios, "full"),
            (["scripts/molecule.py"], all_scenarios, "full"),
            (["scripts/ci/molecule-impact.json"], all_scenarios, "full"),
            ([".github/workflows/ci.yml"], all_scenarios, "full"),
            (["requirements.yml"], all_scenarios, "full"),
            (["unknown.txt"], all_scenarios, "full"),
            ([], all_scenarios, "full"),
            (["README.md"], set(), "none"),
            (["playbooks/reverse-proxy/README.md"], set(), "none"),
            (["roles/reverse_proxy/README.md"], set(), "none"),
            (["inventory/production/hosts.yml"], set(), "none"),
        ]
        for paths, selectors, mode in cases:
            with self.subTest(paths=paths):
                plan = self.plan(paths)
                self.assertEqual(mode, plan["mode"])
                self.assertEqual(
                    {
                        (selector, platform)
                        for selector in selectors
                        for platform in ("debian13", "rockylinux9")
                    },
                    {
                        (row["selector"], row["platform"])
                        for row in plan["matrix"]["include"]
                    },
                )
                for row in plan["matrix"]["include"]:
                    self.assertTrue(row["reasons"])

    def test_union_and_reasons_are_deterministic(self):
        paths = [
            "roles/tls_automation/tasks/main.yml",
            "roles/system_maintenance/tasks/main.yml",
        ]
        plan = self.plan(paths)
        self.assertEqual(plan, self.plan(list(reversed(paths)) + paths))
        baseline = next(
            row
            for row in plan["matrix"]["include"]
            if row["selector"] == "system_maintenance/baseline"
        )
        self.assertTrue(
            all(any(path in reason for reason in baseline["reasons"]) for path in paths)
        )

    def test_forced_depth_cannot_produce_empty_molecule_suite(self):
        for depth in ("molecule", "full"):
            with self.subTest(depth=depth):
                plan = self.plan(["README.md"], depth)
                self.assertEqual("full", plan["mode"])
                self.assertEqual(6, len(plan["matrix"]["include"]))

    def test_unknown_molecule_impact_fails_closed(self):
        import molecule_plan

        result = {"depth": "molecule", "paths": ["roles/future/tasks/main.yml"]}
        plan = molecule_plan.build_plan(result)
        self.assertEqual("full", plan["mode"])
        self.assertEqual(6, len(plan["matrix"]["include"]))

    def test_missing_scenario_rule_does_not_inherit_role_mapping(self):
        import molecule_plan

        document = json.loads(molecule_plan.MAP_PATH.read_text())
        document["rules"] = [
            rule
            for rule in document["rules"]
            if "roles/reverse_proxy/molecule/default/" not in rule["prefixes"]
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            path.write_text(json.dumps(document))
            result = classify.classify_paths(
                [
                    "roles/reverse_proxy/molecule/default/verify.yml",
                    "roles/system_maintenance/tasks/main.yml",
                ]
            )
            plan = molecule_plan.build_plan(result, map_path=path)
            self.assertEqual("full", plan["mode"])
            self.assertEqual(6, len(plan["matrix"]["include"]))

    def test_invalid_map_falls_back_to_runner_registry(self):
        import molecule_plan

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            for content in (
                None,
                "{",
                "{}",
                '{"rules": []}',
                '{"rules": [{"prefixes": ["roles/"], "selectors": ["missing"]}]}',
            ):
                with self.subTest(content=content):
                    if content is not None:
                        path.write_text(content)
                    plan = molecule_plan.build_plan(
                        classify.classify_paths(["roles/reverse_proxy/tasks/main.yml"]),
                        map_path=path,
                    )
                    self.assertEqual("full", plan["mode"])
                    self.assertEqual(6, len(plan["matrix"]["include"]))

    def test_merge_gate_checks_plan_completeness(self):
        import merge_gate

        valid = self.plan(["roles/reverse_proxy/tasks/main.yml"])
        self.assertEqual([], merge_gate.plan_errors("molecule", json.dumps(valid)))
        full = self.plan(["requirements.yml"])
        self.assertEqual([], merge_gate.plan_errors("full", json.dumps(full)))
        none = self.plan(["README.md"])
        self.assertEqual([], merge_gate.plan_errors("fast", json.dumps(none)))
        cases = [
            ("molecule", ""),
            ("molecule", "{"),
            ("molecule", "null"),
            ("molecule", json.dumps(none)),
            ("full", json.dumps(valid)),
            ("fast", json.dumps(valid)),
        ]
        one_platform = json.loads(json.dumps(valid))
        one_platform["matrix"]["include"].pop()
        cases.append(("molecule", json.dumps(one_platform)))
        duplicate = json.loads(json.dumps(valid))
        duplicate["matrix"]["include"].append(duplicate["matrix"]["include"][0])
        cases.append(("molecule", json.dumps(duplicate)))
        for depth, payload in cases:
            with self.subTest(depth=depth, payload=payload):
                self.assertTrue(merge_gate.plan_errors(depth, payload))


if __name__ == "__main__":
    unittest.main()
