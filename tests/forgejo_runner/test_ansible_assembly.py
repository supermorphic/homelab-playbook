import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml
from test_policy import slot

ROOT = Path(__file__).parents[2]
WORKTREE = Path.cwd()


class AnsibleAssemblyTests(unittest.TestCase):
    def test_actual_inputs_and_templates_assemble_three_scoped_slots_without_host_mutation(self):
        scratch = ROOT / '.tmp'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as name:
            directory = Path(name)
            defaults = yaml.safe_load((ROOT / 'roles/forgejo_runner/defaults/main.yml').read_text())
            sources = {key: defaults['forgejo_runner_candidate'][mapping]['amd64']
                       for key, mapping in (('probe_image', 'probe_images'), ('runner_image', 'runner_images'),
                                            ('mise_image', 'mise_images'))}
            artifacts = {}
            for asset in ('job.oci', 'forgejo-runner', 'netavark'):
                payload = ('declaration-test-only-' + asset).encode()
                (directory / asset).write_bytes(payload)
                artifacts[asset] = {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
            variables = {
                'forgejo_runner_slots': [slot(), slot(1, 'supermorphic/homelab-talos'),
                                         slot(2, 'supermorphic/career-ops')],
                'forgejo_runner_asset_directory': str(directory.resolve()),
                'forgejo_runner_source_commit': 'a'*40,
                'forgejo_runner_asset_manifest': {'schema': 1, 'architecture': 'amd64', 'source_tree': 'b'*40,
                    'job_image_id': 'c'*64, 'sources': sources, 'registry_images': ['docker.io/library/debian:13'],
                    'artifacts': artifacts}}
            variables_file = directory / 'variables.json'
            variables_file.write_text(json.dumps(variables))
            result_file = directory / 'assembled.json'
            playbook = directory / 'assemble.yml'
            playbook.write_text(yaml.safe_dump([{'name': 'Assemble public runner declarations',
                'hosts': 'localhost', 'gather_facts': False, 'tasks': [
                    {'name': 'Evaluate actual role inputs', 'ansible.builtin.include_role':
                     {'name': 'forgejo_runner', 'tasks_from': 'inputs.yml', 'public': True}},
                    {'name': 'Render actual role definitions', 'ansible.builtin.include_role':
                     {'name': 'forgejo_runner', 'tasks_from': 'definitions.yml'}},
                    {'name': 'Capture public declaration results', 'ansible.builtin.copy':
                     {'content': '{{ forgejo_runner_rendered_files | to_json }}', 'dest': str(result_file.resolve()),
                      'mode': '0600'}}]}], sort_keys=False))
            configuration = directory / 'ansible.cfg'
            configuration.write_text('[defaults]\nroles_path = ' + str(ROOT.resolve() / 'roles')
                                     + ':' + str(WORKTREE / 'roles') + '\nvars_plugins_enabled=host_group_vars\n')
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('SOPS_AGE_', 'ANSIBLE_SOPS_', 'ANSIBLE_VAULT_'))}
            environment.update(ANSIBLE_CONFIG=str(configuration.resolve()), ANSIBLE_VARS_ENABLED='host_group_vars',
                SOPS_ANSIBLE_AWX_DISABLE_VARS_PLUGIN_TEMPORARILY='true', PYTHONPATH=str(WORKTREE))
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local',
                str(playbook.resolve()), '-e', '@' + str(variables_file.resolve())],
                env=environment, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            files = json.loads(result_file.read_text())
            configurations = [json.loads(row['content']) for row in files if row['path'].endswith('/config.json')]
            self.assertEqual(3, len(configurations))
            self.assertEqual({slot()['repository'], 'supermorphic/homelab-talos', 'supermorphic/career-ops'},
                             {row['slot']['repository'] for row in configurations})
            self.assertTrue(all(row['slot']['enabled'] is False for row in configurations))
            self.assertTrue(all(row['mode'] == 0o600 for row in files if row['path'].endswith('.json')))
            self.assertEqual(3, sum(row['path'].endswith('.service') for row in files))
            self.assertFalse(any(row['path'].endswith('/enrollment-token') for row in files))
