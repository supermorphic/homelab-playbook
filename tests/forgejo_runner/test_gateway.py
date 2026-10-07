import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).parents[2]


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.scratch = Path(self.directory.name)
        self.log = self.scratch / 'calls'
        tool = self.scratch / 'uv'
        tool.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GATEWAY_CALLS"\n'
                        'case "$*" in *ansible-config*) printf "%s\\n" "$GATEWAY_CONFIG";; esac\n')
        tool.chmod(0o700)
        self.environment = {**os.environ, 'PATH': str(self.scratch) + ':' + os.environ['PATH'],
                            'GATEWAY_CALLS': str(self.log), 'GATEWAY_CONFIG': ''}

    def run_gateway(self, *arguments):
        return subprocess.run(['bash', str(ROOT / 'scripts/playbook.sh'), 'forgejo-runner', *arguments],
                              env=self.environment, text=True, capture_output=True)

    def test_all_six_actions_reach_the_canonical_production_playbook(self):
        for action in ('provision', 'verify', 'drain', 'rotate', 'retire', 'recover'):
            with self.subTest(action=action):
                result = self.run_gateway(action, 'production', '--limit', 'nuc4', '--check')
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('/playbooks/forgejo-runner/' + action + '.yml --limit nuc4 --check', self.log.read_text())

    def test_unsupported_inventories_and_password_or_task_selection_never_invoke_ansible(self):
        for action in ('provision', 'verify', 'drain', 'rotate', 'retire', 'recover'):
            for inventory in ('staging', 'frozen/k3s'):
                with self.subTest(action=action, inventory=inventory):
                    self.assertEqual(2, self.run_gateway(action, inventory).returncode)
                    self.assertFalse(self.log.exists())
        for arguments in (('-K',), ('--ask-pass',), ('--start-at-task=x',), ('--tags=unsafe',),
                          ('--skip-tags', 'guard'), ('--become-password-file=x',)):
            with self.subTest(arguments=arguments):
                self.assertEqual(2, self.run_gateway('provision', 'production', *arguments).returncode)
                self.assertFalse(self.log.exists())

    def test_effective_password_configuration_is_rejected_before_playbook_execution(self):
        self.environment['GATEWAY_CONFIG'] = 'DEFAULT_ASK_PASS(env: ANSIBLE_ASK_PASS) = True'
        self.assertEqual(2, self.run_gateway('provision', 'production').returncode)
        self.assertNotIn('ansible-playbook', self.log.read_text())
