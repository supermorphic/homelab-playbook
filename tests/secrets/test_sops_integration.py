"""Exercise repository vars loading with disposable keys and localhost only."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class SopsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sops-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        # Do not inherit credentials, plugin overrides, HOME, or user config.
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "ANSIBLE_CONFIG": str(ROOT / "ansible.cfg"),
            "ANSIBLE_LOCAL_TEMP": str(self.root / "ansible-tmp"),
            "ANSIBLE_NOCOLOR": "1",
        }
        self.key = self.root / "identity"
        self.recipient = self.create_identity(self.key)
        self.env["ANSIBLE_SOPS_AGE_KEY_CMD"] = f"cat {shlex.quote(str(self.key))}"
        policy = yaml.safe_load((ROOT / ".sops.yaml").read_text())
        policy["creation_rules"][0]["age"] = self.recipient
        self.config = self.root / ".sops.yaml"
        self.config.write_text(yaml.safe_dump(policy))
        self.inventory = self.root / "inventory" / "staging"
        group = self.inventory / "group_vars" / "fixture"
        host = self.inventory / "host_vars" / "fixture-host"
        group.mkdir(parents=True)
        host.mkdir(parents=True)
        (self.inventory / "hosts.yml").write_text(
            "all:\n  children:\n    fixture:\n      hosts:\n"
            "        fixture-host:\n          ansible_connection: local\n"
        )
        (group / "vars.yml").write_text("public_value: visible\n")
        self.group_secret = group / "secrets.sops.yml"
        self.encrypt(self.group_secret, {
            "group_secret": "ephemeral-group-value",
            "precedence": "group",
            "templated": "{{ public_value }}-from-secret",
            "nested": {"list": ["ephemeral", 42, True]},
        })
        self.encrypt(host / "secrets.sops.yml", {
            "host_secret": "ephemeral-host-value", "precedence": "host",
        })
        self.playbook = self.root / "playbook.yml"
        self.playbook.write_text("""---
- name: Prove automatic encrypted inventory loading
  hosts: fixture
  gather_facts: false
  tasks:
    - name: Assert public and decrypted variables without disclosure
      ansible.builtin.assert:
        that:
          - public_value == 'visible'
          - group_secret == 'ephemeral-group-value'
          - host_secret == 'ephemeral-host-value'
          - precedence == 'host'
          - templated == 'visible-from-secret'
          - nested.list == ['ephemeral', 42, true]
        quiet: true
      no_log: true
""")

    def run_command(self, command, **kwargs):
        return subprocess.run(command, cwd=ROOT, env=self.env, capture_output=True,
                              timeout=60, **kwargs)

    def create_identity(self, target):
        result = self.run_command(["age-keygen"])
        self.assertEqual(0, result.returncode, "ephemeral identity generation failed")
        with target.open("xb") as output:
            target.chmod(0o600)
            output.write(result.stdout)
        recipient = self.run_command(["age-keygen", "-y", str(target)])
        self.assertEqual(0, recipient.returncode)
        return recipient.stdout.decode().strip()

    def encrypt(self, target, value):
        result = self.run_command([
            "sops", "--encrypt", "--config", str(self.config),
            "--filename-override", str(self.root / "inventory/staging/group_vars/fixture/secrets.sops.yml"),
            "--input-type", "yaml", "--output-type", "yaml", "/dev/stdin",
        ], input=yaml.safe_dump(value).encode())
        self.assertEqual(0, result.returncode, result.stderr.decode())
        target.write_bytes(result.stdout)

    def play(self):
        return self.run_command([
            str(ROOT / ".venv/bin/ansible-playbook"),
            "-i", str(self.inventory / "hosts.yml"), str(self.playbook),
        ])

    def test_group_host_public_precedence_and_templates(self):
        result = self.play()
        self.assertEqual(0, result.returncode, (result.stdout + result.stderr).decode())
        self.assertIn(b"failed=0", result.stdout)
        import importlib.util
        spec = importlib.util.spec_from_file_location("encrypted_validation", ROOT / "scripts/secrets/validate.py")
        validation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validation)
        self.assertEqual([], validation.validate_encrypted_file(self.group_secret, {self.recipient}))

    def test_service_files_load_together_with_shared_routes(self):
        host = self.inventory / "host_vars" / "fixture-host"
        self.encrypt(host / "tls.sops.yml", {"tls_automation_namespace": "infra.example.com"})
        self.encrypt(host / "reverse-proxy.sops.yml", {"reverse_proxy_routes": [
            {"hostname": "semaphore.infra.example.com", "backend_port": 18080,
             "certificate_name": "infra"},
            {"hostname": "forgejo.infra.example.com", "backend_port": 18081,
             "certificate_name": "infra"},
        ]})
        self.encrypt(host / "semaphore.sops.yml", {"semaphore_database_password": "fixture-semaphore"})
        self.encrypt(host / "forgejo.sops.yml", {
            "forgejo_database_password": "fixture-forgejo",
            "forgejo_hostname": "forgejo.{{ tls_automation_namespace }}",
        })
        with self.playbook.open("a") as output:
            output.write("""
    - name: Prove sibling service files share one host scope
      ansible.builtin.assert:
        that:
          - tls_automation_namespace == 'infra.example.com'
          - semaphore_database_password == 'fixture-semaphore'
          - forgejo_database_password == 'fixture-forgejo'
          - forgejo_hostname == 'forgejo.infra.example.com'
          - reverse_proxy_routes | length == 2
          - reverse_proxy_routes[0].hostname == 'semaphore.infra.example.com'
          - reverse_proxy_routes[1].hostname == forgejo_hostname
        quiet: true
      no_log: true
""")
        result = self.play()
        self.assertEqual(0, result.returncode, (result.stdout + result.stderr).decode())

    def test_modified_service_file_mac_fails(self):
        target = self.inventory / "host_vars" / "fixture-host" / "forgejo.sops.yml"
        self.encrypt(target, {"forgejo_database_password": "fixture-forgejo"})
        document = yaml.safe_load(target.read_bytes())
        document["unexpected"] = "unauthenticated-value"
        target.write_text(yaml.safe_dump(document))
        self.assertNotEqual(0, self.play().returncode)

    def test_unavailable_identity_fails(self):
        self.env["ANSIBLE_SOPS_AGE_KEY_CMD"] = "/usr/bin/false"
        self.assertNotEqual(0, self.play().returncode)

    def test_wrong_identity_fails(self):
        other = self.root / "other-identity"
        self.create_identity(other)
        self.env["ANSIBLE_SOPS_AGE_KEY_CMD"] = f"cat {shlex.quote(str(other))}"
        self.assertNotEqual(0, self.play().returncode)

    def test_modified_ciphertext_fails(self):
        encrypted = yaml.safe_load(self.group_secret.read_bytes())
        value = encrypted["group_secret"]
        prefix, data = value.split("data:", 1)
        encrypted["group_secret"] = prefix + "data:" + ("B" if data[0] == "A" else "A") + data[1:]
        self.group_secret.write_text(yaml.safe_dump(encrypted))
        self.assertNotEqual(0, self.play().returncode)

    def test_plaintext_at_encrypted_path_fails(self):
        self.group_secret.write_text("group_secret: ephemeral-group-value\n")
        self.assertNotEqual(0, self.play().returncode)

    def test_selected_wrong_identity_cannot_fall_back_to_operator_keys(self):
        # A correct competing identity must not rescue the wrong selected command.
        other = self.root / "selected-wrong-identity"
        self.create_identity(other)
        self.env["ANSIBLE_SOPS_AGE_KEY_CMD"] = f"cat {shlex.quote(str(other))}"
        self.env["SOPS_AGE_KEY"] = self.key.read_text()
        self.env["SOPS_AGE_KEY_FILE"] = str(self.key)
        for directory in (self.root / "config/sops/age",
                          self.root / "Library/Application Support/sops/age"):
            directory.mkdir(parents=True)
            (directory / "keys.txt").write_bytes(self.key.read_bytes())
        self.assertNotEqual(0, self.play().returncode)

    def test_missing_command_cannot_fall_back_to_operator_key(self):
        self.env["ANSIBLE_SOPS_AGE_KEY_CMD"] = "/usr/bin/false"
        self.env["SOPS_AGE_KEY"] = self.key.read_text()
        self.assertNotEqual(0, self.play().returncode)

    def test_retrieval_command_keeps_original_home(self):
        self.env["ANSIBLE_SOPS_AGE_KEY_CMD"] = 'sh -c \'cat "$HOME/identity"\''
        result = self.play()
        self.assertEqual(0, result.returncode, (result.stdout + result.stderr).decode())

    def test_direct_sops_editor_keeps_operator_home(self):
        editor = self.root / "fixture-editor.sh"
        marker = self.root / "editor-context"
        editor.write_text('#!/bin/sh\nprintf "%s\\n%s\\n" "$HOME" "$XDG_CONFIG_HOME" > "$HOME/editor-context"\n')
        editor.chmod(0o700)
        self.env["SOPS_AGE_KEY_CMD"] = f"cat {shlex.quote(str(self.key))}"
        self.env["SOPS_EDITOR"] = shlex.quote(str(editor))
        result = self.run_command([str(ROOT / "scripts/secrets/sops.sh"), "--config", str(self.config), str(self.group_secret)])
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(f"{self.root}\n{self.root / 'config'}\n", marker.read_text())

    def test_unchanged_edit_succeeds_without_rewriting_ciphertext(self):
        original = self.group_secret.read_bytes()
        self.env["SOPS_AGE_KEY_CMD"] = f"cat {shlex.quote(str(self.key))}"
        self.env["SOPS_EDITOR"] = "/usr/bin/true"
        result = self.run_command([
            str(ROOT / "scripts/secrets/sops.sh"), "--config", str(self.config),
            str(self.group_secret),
        ])
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertIn(b"File has not changed, exiting.", result.stderr)
        self.assertEqual(original, self.group_secret.read_bytes())

    def test_editor_failure_preserves_failure_status_and_ciphertext(self):
        original = self.group_secret.read_bytes()
        self.env["SOPS_AGE_KEY_CMD"] = f"cat {shlex.quote(str(self.key))}"
        self.env["SOPS_EDITOR"] = "/usr/bin/false"
        result = self.run_command([
            str(ROOT / "scripts/secrets/sops.sh"), "--config", str(self.config),
            str(self.group_secret),
        ])
        self.assertEqual(201, result.returncode, result.stderr.decode())
        self.assertEqual(original, self.group_secret.read_bytes())

    def test_independent_second_recipient_can_decrypt_after_rewrap(self):
        other = self.root / "controller-identity"
        other_recipient = self.create_identity(other)
        policy = yaml.safe_load(self.config.read_bytes())
        policy["creation_rules"][0]["age"] += f",{other_recipient}"
        self.config.write_text(yaml.safe_dump(policy))
        self.env["SOPS_AGE_KEY_CMD"] = f"cat {shlex.quote(str(self.key))}"
        for source in self.inventory.rglob("*.sops.yml"):
            result = self.run_command([
                str(ROOT / "scripts/secrets/sops.sh"), "--config", str(self.config),
                "updatekeys", "--yes", str(source),
            ])
            self.assertEqual(0, result.returncode, result.stderr.decode())
        self.env["ANSIBLE_SOPS_AGE_KEY_CMD"] = f"cat {shlex.quote(str(other))}"
        result = self.play()
        self.assertEqual(0, result.returncode, (result.stdout + result.stderr).decode())
