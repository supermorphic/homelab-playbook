"""Evaluate actual role assertions against synthetic input, without inventory."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
INPUTS = ROOT / "roles/forgejo/tasks/inputs.yml"


class InputTests(unittest.TestCase):
    def settings(self):
        settings = yaml.safe_load((ROOT / "roles/forgejo/defaults/main.yml").read_text())
        settings.update({
            "forgejo_foundation_account": {
                "name": "svc-forgejo", "uid": 2002, "gid": 2002,
                "subuid_start": 200000, "subuid_count": 65536,
                "subgid_start": 200000, "subgid_count": 65536,
            },
            "forgejo_hostname": "forgejo.infra.example.com",
            "forgejo_proxy_source": "127.0.0.1/32",
            "forgejo_rclone_remote": "nas:fixture-forgejo",
            "reverse_proxy_routes": [{"hostname": "forgejo.infra.example.com",
                "backend_port": 18081, "certificate_name": "infra"}],
        })
        for name in ("database_admin_password", "database_password", "admin_password",
                     "secret_key", "internal_token", "lfs_jwt_secret", "oauth2_jwt_secret"):
            settings["forgejo_" + name] = "synthetic-only-not-live"
        settings.update(forgejo_admin_login="fixture-admin",
                        forgejo_admin_email="fixture@example.invalid",
                        forgejo_rclone_config="[nas]\ntype = smb\nhost = fixture\n")
        return settings

    def evaluate(self, values):
        self.assertTrue(INPUTS.is_file(), "Forgejo input assertions are missing")
        scratch_root = ROOT / ".tmp/forgejo"
        scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch_root) as name:
            directory = Path(name)
            config = directory / "ansible.cfg"
            config.write_text("[defaults]\nvars_plugins_enabled=host_group_vars\nretry_files_enabled=false\n")
            variables = directory / "vars.json"
            variables.write_text(json.dumps(values))
            variables.chmod(0o600)
            playbook = directory / "test.yml"
            playbook.write_text(yaml.safe_dump([{
                "name": "Validate synthetic Forgejo inputs", "hosts": "localhost",
                "gather_facts": False, "tasks": [{"name": "Evaluate input assertions",
                    "ansible.builtin.import_tasks": str(INPUTS)}],
            }]))
            environment = {key: value for key, value in os.environ.items()
                if not key.startswith(("SOPS_AGE_", "ANSIBLE_SOPS_", "ANSIBLE_VAULT_"))}
            environment.update(ANSIBLE_CONFIG=str(config),
                ANSIBLE_VARS_ENABLED="host_group_vars",
                SOPS_ANSIBLE_AWX_DISABLE_VARS_PLUGIN_TEMPORARILY="true")
            result = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local",
                str(playbook), "-e", "@" + str(variables)], cwd=directory,
                env=environment, capture_output=True, text=True, check=False, timeout=30)
            self.assertNotIn("synthetic-only-not-live", result.stdout + result.stderr)
            return result.returncode

    def test_declared_synthetic_inputs_are_accepted(self):
        self.assertEqual(0, self.evaluate(self.settings()))

    def test_missing_allocation_is_rejected(self):
        values = self.settings()
        values["forgejo_foundation_account"] = {}
        self.assertNotEqual(0, self.evaluate(values))

    def test_wrong_proxy_route_is_rejected(self):
        values = self.settings()
        values["reverse_proxy_routes"][0]["backend_port"] = 18080
        self.assertNotEqual(0, self.evaluate(values))

    def test_missing_protected_input_is_rejected_without_disclosure(self):
        values = self.settings()
        del values["forgejo_secret_key"]
        self.assertNotEqual(0, self.evaluate(values))

    def test_wildcard_proxy_source_is_rejected(self):
        values = self.settings()
        values["forgejo_proxy_source"] = "*"
        self.assertNotEqual(0, self.evaluate(values))

    def test_invalid_ipv4_proxy_source_is_rejected(self):
        values = self.settings()
        values["forgejo_proxy_source"] = "999.0.0.1/32"
        self.assertNotEqual(0, self.evaluate(values))

    def test_newline_in_protected_single_line_input_is_rejected(self):
        values = self.settings()
        values["forgejo_database_password"] = "synthetic\nsecond-line"
        self.assertNotEqual(0, self.evaluate(values))


if __name__ == "__main__":
    unittest.main()
