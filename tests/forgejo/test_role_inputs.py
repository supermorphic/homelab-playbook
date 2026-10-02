"""Evaluate actual role assertions against synthetic input, without inventory."""

import json
import configparser
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml
from jinja2 import Template

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

    def evaluate(self, values, task_file=INPUTS):
        self.assertTrue(task_file.is_file(), "Forgejo assertions are missing")
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
                    "ansible.builtin.import_tasks": str(task_file)}],
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

    def test_proxy_headers_cannot_supply_client_identity_or_authentication(self):
        values = self.settings()
        template = ROOT / 'roles/forgejo/templates/app.ini.j2'
        config = configparser.ConfigParser(interpolation=None)
        config.read_string('[DEFAULT]\n' + Template(template.read_text()).render(**values))
        self.assertEqual(0, config.getint('security', 'REVERSE_PROXY_LIMIT'))
        self.assertFalse(config.getboolean('service', 'ENABLE_REVERSE_PROXY_AUTHENTICATION'))
        self.assertEqual('https://forgejo.infra.example.com/', config['server']['ROOT_URL'])

    def test_unbounded_backup_budget_is_rejected(self):
        values = self.settings()
        values["forgejo_backup_timeout_seconds"] = 7200
        self.assertNotEqual(0, self.evaluate(values))

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

    def test_newline_in_protected_single_line_input_is_rejected(self):
        values = self.settings()
        values["forgejo_database_password"] = "synthetic\nsecond-line"
        self.assertNotEqual(0, self.evaluate(values))

    def test_verify_detects_private_input_drift_without_repair(self):
        values = self.settings()
        root = ROOT / ".tmp/forgejo"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            state = Path(directory)
            values["forgejo_state_root"] = str(state)
            for folder in ("config", "credentials"):
                (state / folder).mkdir(mode=0o700)
            names = {
                "config/database-password": "database_password",
                "config/secret-key": "secret_key",
                "config/internal-token": "internal_token",
                "config/lfs-jwt-secret": "lfs_jwt_secret",
                "config/oauth2-jwt-secret": "oauth2_jwt_secret",
                "credentials/postgres-admin-password": "database_admin_password",
                "config/mirror-credentials": "mirror_credentials",
            }
            for path, variable in names.items():
                target = state / path
                target.write_text(values["forgejo_" + variable])
                target.chmod(0o600)
            task_file = ROOT / "roles/forgejo/tasks/verify-definitions.yml"
            self.assertEqual(0, self.evaluate(values, task_file),
                             "matching private inputs must pass observational comparison")
            changed = state / "config/secret-key"
            changed.write_text("different-synthetic-value")
            code = self.evaluate(values, task_file)
            self.assertNotEqual(0, code)
            self.assertEqual("different-synthetic-value", changed.read_text())

    def test_verify_detects_public_declaration_drift_without_repair(self):
        values = self.settings()
        root = ROOT / ".tmp/forgejo"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            state = Path(directory)
            values["forgejo_helper_root"] = str(state)
            manifest = {"schema": 1, "allocation": values["forgejo_foundation_account"],
                "hostname": values["forgejo_hostname"], "backend_port": values["forgejo_backend_port"],
                "health_timeout_seconds": values["forgejo_health_timeout_seconds"],
                "image": values["forgejo_image"], "postgres_image": values["forgejo_postgres_image"],
                "rclone_image": values["forgejo_rclone_image"],
                "rclone_remote": values["forgejo_rclone_remote"]}
            target = state / "desired.json"
            target.write_text(json.dumps(manifest))
            task_file = ROOT / "roles/forgejo/tasks/verify-public.yml"
            self.assertEqual(0, self.evaluate(values, task_file))
            values["forgejo_hostname"] = "other.infra.example.com"
            self.assertNotEqual(0, self.evaluate(values, task_file))
            self.assertEqual(manifest, json.loads(target.read_text()))


if __name__ == "__main__":
    unittest.main()
