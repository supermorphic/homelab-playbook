"""Reject unsafe identity allocations before host mutation."""

import importlib.util
from pathlib import Path
import unittest
import subprocess
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[2]
# Explicit local hosts would discover a PATH interpreter; use the controller's, like implicit localhost.
CONTROLLER_PYTHON = ("-e", "ansible_python_interpreter={{ ansible_playbook_python }}")
SOURCE = ROOT / "roles/podman_foundation/filter_plugins/identity.py"


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.account = dict(
            name="svc-example",
            uid=2001,
            gid=2001,
            subuid_start=200000,
            subuid_count=65536,
            subgid_start=200000,
            subgid_count=65536,
        )

    def validate(self, accounts=None, **state):
        self.assertTrue(SOURCE.exists(), "foundation identity validator is missing")
        spec = importlib.util.spec_from_file_location("identity", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.validate_accounts(
            [self.account] if accounts is None else accounts,
            state.get("passwd", "root:x:0:0:root:/root:/bin/bash\n"),
            state.get("group", "root:x:0:\n"),
            state.get("subuid", ""),
            state.get("subgid", ""),
            state.get("require_present", False),
            state.get("pending", []),
        )

    def test_empty_declaration_creates_no_future_accounts(self):
        self.assertEqual([], self.validate([]))

    def test_overlapping_unmanaged_ranges_fail_even_with_empty_declarations(self):
        for key in ("subuid", "subgid"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "overlap"):
                self.validate([], **{key: "old-a:500000:65536\nold-b:565535:65536\n"})

    def test_unmanaged_ranges_cannot_include_effective_host_identities(self):
        cases = [
            dict(
                passwd="other:x:200010:10::/home/other:/bin/sh\n",
                subuid="old:200000:65536\n",
            ),
            dict(group="other:x:200010:\n", subgid="old:200000:65536\n"),
            dict(
                passwd="other:x:10:200010::/home/other:/bin/sh\n",
                subgid="old:200000:65536\n",
            ),
        ]
        for state in cases:
            with (
                self.subTest(state=state),
                self.assertRaisesRegex(ValueError, "overlap"),
            ):
                self.validate([], **state)

    def test_file_subid_provider_handles_whitespace_and_rejects_ambiguity(self):
        spec = importlib.util.spec_from_file_location("identity", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, "file_subid_provider"))
        for value in (
            "passwd: files\n",
            "  subid: files  # local\n",
            "\tsubid:\tfiles\n",
        ):
            self.assertTrue(module.file_subid_provider(value))
        for value in (
            "  subid: sss\n",
            "subid: files sss\n",
            "subid: files\nsubid: files\n",
            "subid:\n",
        ):
            self.assertFalse(module.file_subid_provider(value))

    def test_new_account_keeps_explicit_ids(self):
        self.assertEqual([self.account], self.validate())

    def test_ansible_yaml_numeric_values_are_valid(self):
        from ansible.parsing.dataloader import DataLoader

        declared = DataLoader().load(
            "- {name: svc-example, uid: 2001, gid: 2001, "
            "subuid_start: 200000, subuid_count: 65536, "
            "subgid_start: 200000, subgid_count: 65536}"
        )
        self.assertEqual([self.account], self.validate(declared))

    def test_duplicate_host_id_is_rejected(self):
        other = dict(
            self.account, name="svc-other", subuid_start=300000, subgid_start=300000
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.validate([self.account, other])

    def test_subordinate_endpoint_overlap_is_rejected(self):
        other = dict(
            self.account,
            name="svc-other",
            uid=2002,
            gid=2002,
            subuid_start=265535,
            subgid_start=300000,
        )
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.validate([self.account, other])

    def test_adjacent_ranges_are_valid(self):
        other = dict(
            self.account,
            name="svc-other",
            uid=2002,
            gid=2002,
            subuid_start=265536,
            subgid_start=265536,
        )
        self.assertEqual(2, len(self.validate([self.account, other])))

    def test_unmanaged_range_and_real_uid_conflicts_are_rejected(self):
        for state in [
            dict(subuid="other:220000:65536\n"),
            dict(subgid="other:220000:65536\n"),
            dict(passwd="other:x:200010:100:Other:/home/other:/bin/sh\n"),
        ]:
            with (
                self.subTest(state=state),
                self.assertRaisesRegex(ValueError, "overlap"),
            ):
                self.validate(**state)

    def test_existing_account_never_renumbered(self):
        with self.assertRaisesRegex(ValueError, "migration"):
            self.validate(
                passwd="svc-example:x:2002:2001::/var/lib/svc-example:/usr/sbin/nologin\n"
            )

    def test_existing_account_requires_exact_subordinate_maps(self):
        with self.assertRaisesRegex(ValueError, "migration"):
            self.validate(
                passwd="svc-example:x:2001:2001::/var/lib/svc-example:/usr/sbin/nologin\n",
                group="svc-example:x:2001:\n",
            )

    def test_existing_numeric_owner_maps_are_accepted(self):
        self.assertEqual(
            [self.account],
            self.validate(
                passwd="svc-example:x:2001:2001::/var/lib/svc-example:/usr/sbin/nologin\n",
                group="svc-example:x:2001:\n",
                subuid="2001:200000:65536\n",
                subgid="2001:200000:65536\n",
                require_present=True,
            ),
        )

    def test_verify_requires_declared_account_to_exist(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            self.validate(require_present=True)

    def test_invalid_fields_and_ranges_fail_closed(self):
        for change in [
            dict(uid=True),
            dict(name="ansible"),
            dict(name="../bad"),
            dict(subgid_count=1),
            dict(subuid_start=4294967290),
            dict(unexpected="value"),
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate([dict(self.account, **change)])


class SudoVerificationTests(unittest.TestCase):
    def test_only_explicit_subject_denial_proves_no_sudo_grants(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        tasks = yaml.safe_load(
            (ROOT / "roles/podman_foundation/tasks/verify-account.yml").read_text()
        )
        assertion = next(
            task
            for task in tasks
            if task["name"] == "Verify service account has no sudo authorization"
        )
        cases = [
            (0, "User svc-example is not allowed to run sudo on fixture.\n", True),
            (1, "User svc-example is not allowed to run sudo on fixture.\n", True),
            (
                0,
                "User svc-example may run the following commands on fixture:\n    (ALL) ALL\n",
                False,
            ),
            (1, "", False),
            (0, "User svc-other is not allowed to run sudo on fixture.\n", False),
        ]
        for rc, output, expected in cases:
            with (
                self.subTest(rc=rc, output=output),
                tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory,
            ):
                play = [
                    {
                        "name": "Probe observational sudo assertion",
                        "hosts": "localhost",
                        "connection": "local",
                        "gather_facts": False,
                        "vars": {
                            "podman_foundation_account": {"name": "svc-example"},
                            "podman_foundation_sudo": {
                                "rc": rc,
                                "stdout": output,
                                "stderr": "",
                            },
                        },
                        "tasks": [assertion],
                    }
                ]
                path = Path(directory) / "probe.yml"
                path.write_text(yaml.safe_dump(play))
                result = subprocess.run(
                    ["ansible-playbook", "-i", "localhost,", *CONTROLLER_PYTHON, str(path)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    expected, result.returncode == 0, result.stdout + result.stderr
                )


class PodmanPlaybookCredentialGuardTests(unittest.TestCase):
    PLAYBOOKS = (
        "playbooks/podman/provision.yml",
        "playbooks/podman/verify.yml",
    )
    PASSWORD_ALIASES = (
        "ansible_password",
        "ansible_ssh_pass",
        "ansible_ssh_password",
        "ansible_become_password",
        "ansible_become_pass",
    )

    def load_play(self, relative_path):
        return yaml.safe_load((ROOT / relative_path).read_text())[0]

    def test_credential_guard_precedes_every_remote_task(self):
        expected_conditions = [
            f"{alias} is not defined" for alias in self.PASSWORD_ALIASES
        ]
        for path in self.PLAYBOOKS:
            with self.subTest(path=path):
                pre_tasks = self.load_play(path)["pre_tasks"]
                self.assertGreaterEqual(len(pre_tasks), 3)
                guard, connection_preflight, setup = pre_tasks[:3]
                self.assertEqual(
                    expected_conditions,
                    guard["ansible.builtin.assert"]["that"],
                )
                self.assertIs(guard.get("no_log"), True)
                self.assertEqual(
                    {
                        "name": "os_bootstrap",
                        "tasks_from": "connection-preflight.yml",
                    },
                    connection_preflight["ansible.builtin.import_role"],
                )
                self.assertIn("ansible.builtin.setup", setup)

    def test_every_password_alias_is_rejected_without_echoing_its_value(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        sentinel = "synthetic-password-sentinel"
        for path in self.PLAYBOOKS:
            guard = self.load_play(path)["pre_tasks"][0]
            self.assertIn("ansible.builtin.assert", guard)
            for alias in self.PASSWORD_ALIASES:
                with (
                    self.subTest(path=path, alias=alias),
                    tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory,
                ):
                    play = [
                        {
                            "name": "Probe Podman credential guard",
                            "hosts": "localhost",
                            "connection": "local",
                            "gather_facts": False,
                            "vars": {alias: sentinel},
                            "tasks": [guard],
                        }
                    ]
                    probe = Path(directory) / "probe.yml"
                    probe.write_text(yaml.safe_dump(play))
                    result = subprocess.run(
                        ["ansible-playbook", "-i", "localhost,", *CONTROLLER_PYTHON, str(probe)],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                    )
                    output = result.stdout + result.stderr
                    self.assertNotEqual(0, result.returncode, output)
                    self.assertNotIn(sentinel, output)


if __name__ == "__main__":
    unittest.main()
