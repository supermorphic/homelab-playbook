import importlib.util
from pathlib import Path
import unittest

source = Path(__file__).parents[2] / 'roles/forgejo_runner/files/deployment_dispatch.py'
spec = importlib.util.spec_from_file_location('deployment_dispatch', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DispatchTests(unittest.TestCase):
    def test_private_authority_can_be_restored_for_each_allowed_scope(self):
        for scope in ('user:supermorphic', 'organization:supermorphic'):
            value = {'installation': '/usr/local/libexec/forgejo-runner/' + 'a'*40,
                     'slot': {'name': 'worker-1', 'scope': scope,
                              'state_root': '/var/lib/forgejo-runner/worker-1'}}
            argv = module.arguments('restore-authority', value, value['slot']['state_root'] + '/config.json', scope)
            self.assertEqual(['--scope', scope], argv[-2:])
            with self.assertRaises(ValueError):
                module.arguments('restore-authority', value, value['slot']['state_root'] + '/config.json', 'user:other')

    def test_updates_and_verification_use_the_current_installed_revision(self):
        value = {'installation': '/usr/local/libexec/forgejo-runner/' + 'a'*40,
                     'slot': {'name': 'playbook', 'state_root': '/var/lib/forgejo-runner/playbook'}}
        argv = module.arguments('drain', value, '/var/lib/forgejo-runner/playbook/config.json')
        self.assertEqual('/usr/local/libexec/forgejo-runner/' + 'a'*40 + '/maintenance.py', argv[2])
        self.assertEqual('drain', argv[3])
        argv = module.arguments('verify', value, '/var/lib/forgejo-runner/playbook/config.json')
        self.assertEqual('/usr/local/libexec/forgejo-runner/' + 'a'*40 + '/control.py', argv[2])

    def test_installed_metadata_cannot_redirect_an_administrator_command(self):
        value = {'installation': '/tmp/worker',
                     'slot': {'name': 'playbook', 'state_root': '/var/lib/forgejo-runner/playbook'}}
        with self.assertRaises(ValueError):
            module.arguments('drain', value, '/var/lib/forgejo-runner/playbook/config.json')
        value['installation'] = '/usr/local/libexec/forgejo-runner/' + 'a'*40
        for command in ('cycle', 'shell', '../recover'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                module.arguments(command, value, '/var/lib/forgejo-runner/playbook/config.json')
        with self.assertRaises(ValueError):
            module.arguments('verify', value, '/var/lib/forgejo-runner/other/config.json')
