from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIRECTORY = (
    REPOSITORY_ROOT / "roles" / "system_maintenance" / "molecule" / "default"
)


def load_yaml(name: str):
    path = SCENARIO_DIRECTORY / name
    if not path.is_file():
        raise AssertionError(f"required Molecule scenario file is missing: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_yaml_documents(relative_path: str) -> list[object]:
    path = REPOSITORY_ROOT / relative_path
    if not path.is_file():
        raise AssertionError(f"required YAML file is missing: {path}")
    return list(yaml.safe_load_all(path.read_text(encoding="utf-8")))


class MoleculeScenarioContractTests(unittest.TestCase):
    def test_scenario_uses_the_ansible_native_lifecycle_and_least_privilege(
        self,
    ) -> None:
        configuration = load_yaml("molecule.yml")

        self.assertEqual(
            {"name": "galaxy", "enabled": False},
            configuration["dependency"],
        )
        self.assertEqual(
            {"name": "default", "options": {"managed": True}},
            configuration["driver"],
        )
        self.assertEqual(
            [
                "destroy",
                "syntax",
                "create",
                "prepare",
                "converge",
                "idempotence",
                "verify",
                "cleanup",
                "destroy",
            ],
            configuration["scenario"]["test_sequence"],
        )

        expected_platforms = {
            "debian13": (
                "localhost/homelab-playbook-system-maintenance-debian13:local",
                "homelab-playbook-system-maintenance-debian13",
            ),
            "rockylinux9": (
                "localhost/homelab-playbook-system-maintenance-rockylinux9:local",
                "homelab-playbook-system-maintenance-rockylinux9",
            ),
        }
        platforms = {
            platform["name"]: platform for platform in configuration["platforms"]
        }
        self.assertEqual(set(expected_platforms), set(platforms))
        for name, (image, container_name) in expected_platforms.items():
            with self.subTest(platform=name):
                platform = platforms[name]
                self.assertEqual(image, platform["image"])
                self.assertEqual(container_name, platform["container_name"])
                self.assertEqual(["molecule"], platform["groups"])
                self.assertEqual(
                    "/usr/lib/systemd/systemd",
                    platform["container_command"],
                )
                self.assertIs(platform["container_privileged"], False)
                self.assertEqual("always", platform["container_systemd"])
                self.assertEqual("never", platform["pull"])
                self.assertTrue(
                    {
                        "cap_add",
                        "capabilities",
                        "devices",
                        "volumes",
                    }.isdisjoint(platform)
                )

    def test_create_and_destroy_use_only_the_podman_collection(self) -> None:
        create_plays = load_yaml("create.yml")
        destroy_plays = load_yaml("destroy.yml")

        create_modules = {
            key
            for play in create_plays
            for task in play["tasks"]
            for key in task
            if "." in key
        }
        destroy_modules = {
            key
            for play in destroy_plays
            for task in play["tasks"]
            for key in task
            if "." in key
        }
        self.assertIn("containers.podman.podman_container", create_modules)
        self.assertIn("containers.podman.podman_container", destroy_modules)
        self.assertTrue(
            all("docker" not in module.lower() for module in create_modules)
        )
        self.assertTrue(
            all("docker" not in module.lower() for module in destroy_modules)
        )

    def test_controller_playbooks_select_the_worker_platform_from_environment(
        self,
    ) -> None:
        for name in ("create.yml", "cleanup.yml", "destroy.yml"):
            with self.subTest(playbook=name):
                play = load_yaml(name)[0]
                variables = play["vars"]
                self.assertIn(
                    "lookup('ansible.builtin.env', 'HOMELAB_MOLECULE_PLATFORM')",
                    variables["system_maintenance_molecule_platform_name"],
                )
                self.assertIn(
                    "selectattr('name', 'equalto',",
                    variables["system_maintenance_molecule_platforms"],
                )
                assertions = [
                    task["ansible.builtin.assert"]["that"]
                    for task in play["tasks"]
                    if "ansible.builtin.assert" in task
                ]
                self.assertTrue(
                    any(
                        "system_maintenance_molecule_platforms | length == 1"
                        in conditions
                        for conditions in assertions
                    )
                )

        create_play = load_yaml("create.yml")[0]
        instance_fact = next(
            task["ansible.builtin.set_fact"]
            for task in create_play["tasks"]
            if "ansible.builtin.set_fact" in task
        )
        instance = instance_fact[
            "system_maintenance_molecule_instance_configuration"
        ][0]
        self.assertEqual(
            "{{ system_maintenance_molecule_platform.container_name }}",
            instance["address"],
        )

    def test_converge_suppresses_only_reboot_and_verify_is_independent(self) -> None:
        converge = load_yaml("converge.yml")[0]
        verify = load_yaml("verify.yml")[0]

        self.assertEqual("molecule", converge["hosts"])
        self.assertEqual(
            [
                {
                    "role": "system_maintenance",
                    "system_maintenance_native_reboot_enabled": False,
                }
            ],
            converge["roles"],
        )
        self.assertNotIn("roles", verify)
        self.assertFalse(
            any(
                "ansible.builtin.include_role" in task
                or "ansible.builtin.import_role" in task
                for task in verify["tasks"]
            )
        )

    def test_default_scenario_matches_minimal_maintenance_contract(self) -> None:
        converge = load_yaml_documents(
            "roles/system_maintenance/molecule/default/converge.yml"
        )[0][0]
        role = converge["roles"][0]
        self.assertIs(role["system_maintenance_native_reboot_enabled"], False)
        verify = (
            REPOSITORY_ROOT
            / "roles/system_maintenance/molecule/default/verify.yml"
        ).read_text(encoding="utf-8").lower()
        for retired in ("tree", "vim", "git", "xterm", "epel", "qemu-guest-agent"):
            self.assertNotIn(retired, verify)

    def test_containerfiles_use_maintained_bases_without_role_evidence_package(
        self,
    ) -> None:
        expected_bases = {
            "Containerfile.debian13": "FROM docker.io/library/debian:13",
            "Containerfile.rockylinux9": (
                "FROM docker.io/rockylinux/rockylinux:9"
            ),
        }
        for name, expected_base in expected_bases.items():
            with self.subTest(containerfile=name):
                path = SCENARIO_DIRECTORY / name
                self.assertTrue(path.is_file())
                content = path.read_text(encoding="utf-8")
                self.assertEqual(expected_base, content.splitlines()[0])
                self.assertNotIn("tree", content.lower())
                self.assertIn('CMD ["/usr/lib/systemd/systemd"]', content)
                self.assertIn("STOPSIGNAL SIGRTMIN+3", content)

if __name__ == "__main__":
    unittest.main()
