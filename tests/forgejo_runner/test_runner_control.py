import importlib.util
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
import json
from unittest.mock import patch, Mock

files = Path(__file__).parents[2] / 'roles/forgejo_runner/files'
sys.path.insert(0, str(files))
spec = importlib.util.spec_from_file_location('runner_control', files / 'control.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ControlTests(unittest.TestCase):
    def test_enabled_slot_with_stopped_polling_is_not_reported_ready(self):
        value = {'slot': {'name': 'playbook', 'state_root': '/synthetic', 'enabled': True}}
        with patch.object(module, 'observe', return_value={'verified': True, 'runtime_absent': True}), \
                patch.object(module, 'read_checkpoint', return_value={'registration': None}), \
                patch.object(module, 'execute', return_value='LoadState=loaded\nActiveState=inactive\nUnitFileState=enabled\n'):
            result, code = module.perform('verify', value)
        self.assertEqual(1, code)
        self.assertFalse(result['polling_verified'])

    def test_explicit_runtime_inspection_does_not_require_active_polling(self):
        value = {'slot': {'name': 'playbook', 'state_root': '/synthetic', 'enabled': True}}
        with patch.object(module, 'observe', return_value={'verified': True, 'runtime_absent': True}), \
                patch.object(module, 'read_checkpoint', return_value={'registration': None}), \
                patch.object(module, 'execute') as command:
            result, code = module.perform('inspect', value)
        self.assertEqual(0, code)
        command.assert_not_called()

    def test_missing_enrollment_input_still_disposes_owned_runtime_during_recovery(self):
        import lifecycle
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slot = {'name': 'playbook', 'repository': 'example/project', 'enabled': False,
                    'state_root': str(root), 'label': 'homelab-podman-amd64'}
            slot.pop('repository')
            slot['repositories'] = ['supermorphic/career-ops', 'supermorphic/homelab-playbook', 'supermorphic/homelab-talos']
            def store(path):
                return lifecycle.CheckpointStore(path, authority_uid=os.getuid())
            with store(root) as checkpoint:
                checkpoint.write({**lifecycle.clean_checkpoint(), 'phase': 'preparing', 'generation': 'a'*32})
            runtime = Mock()
            runtime.assert_empty = Mock()
            value = {'slot': slot, 'installation': 'synthetic', 'assets_root': 'synthetic', 'manifest': {}}
            with patch.object(module, 'CheckpointStore', store), \
                    patch.object(module, 'admission_lease', lambda *a, **kw: contextlib.nullcontext(True)), \
                    patch.object(module, 'OwnedRuntime', return_value=runtime):
                result, code = module.perform('recover', value)
            self.assertEqual(1, code)
            self.assertEqual('quarantined', result['phase'])
            runtime.destroy.assert_called_once_with('a'*32, None)
            runtime.assert_empty.assert_called_once()
            with store(root) as checkpoint:
                self.assertEqual('a'*32, checkpoint.read()['generation'])

    def test_busy_host_admission_does_not_load_credentials_or_enroll(self):
        import lifecycle
        with tempfile.TemporaryDirectory() as directory:
            slot = {'name': 'playbook', 'repository': 'example/project', 'enabled': True,
                    'state_root': directory, 'label': 'homelab-podman-amd64'}
            def store(path):
                return lifecycle.CheckpointStore(path, authority_uid=os.getuid())
            value = {'slot': slot}
            with patch.object(module, 'CheckpointStore', store), \
                    patch.object(module, 'admission_lease', lambda *a, **kw: contextlib.nullcontext(False)), \
                    patch.object(module, 'private_token') as token, \
                    patch.object(module, 'RegistrationClient') as client, \
                    patch.object(module, 'OwnedRuntime') as runtime:
                result, code = module.perform('cycle', value)
            self.assertEqual(({'admission_busy': True}, 0), (result, code))
            token.assert_not_called()
            client.assert_not_called()
            runtime.assert_not_called()
            self.assertFalse((Path(directory) / 'state.json').exists())

    def test_restart_cleans_before_polling_another_job(self):
        import lifecycle
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slot = {'name': 'playbook', 'repository': 'example/project', 'enabled': True,
                    'state_root': str(root), 'label': 'homelab-podman-amd64'}
            slot.pop('repository')
            slot['repositories'] = ['supermorphic/career-ops', 'supermorphic/homelab-playbook', 'supermorphic/homelab-talos']
            def store(path):
                return lifecycle.CheckpointStore(path, authority_uid=os.getuid())
            with store(root) as checkpoint:
                checkpoint.write({**lifecycle.clean_checkpoint(), 'phase': 'preparing', 'generation': 'a'*32})
            assignment = root / 'assignment.json'
            assignment.write_text(json.dumps({'repository': 'supermorphic/career-ops', 'generation': 'a'*32,
                                              'job_digest': 'b'*64}))
            assignment.chmod(0o600)
            runtime = Mock()
            runtime.assert_empty = Mock()
            client = Mock()
            client.lookup.return_value = None
            token_reader = lambda path: 'synthetic-token'
            value = {'slot': slot, 'installation': 'synthetic', 'assets_root': 'synthetic', 'manifest': {}}
            with patch.object(module, 'CheckpointStore', store), patch.object(module, 'private_token', token_reader), \
                    patch.object(module, 'RegistrationClient', return_value=client), \
                    patch.object(module, 'admission_lease', lambda *a, **kw: contextlib.nullcontext(True)), \
                    patch.object(module, 'OwnedRuntime', return_value=runtime):
                result, code = module.perform('cycle', value)
            self.assertEqual(0, code)
            client.pending_job.assert_not_called()
            client.enroll.assert_not_called()
            runtime.run.assert_not_called()
            runtime.destroy.assert_called_once_with('a'*32, None)
            with store(root) as checkpoint:
                self.assertEqual('clean', checkpoint.read()['phase'])

    def test_private_token_file_rejects_links_and_public_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'enrollment-token'
            path.write_text('synthetic-token\n')
            path.chmod(0o600)
            self.assertEqual('synthetic-token', module.private_token(path, authority_uid=os.getuid()))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                module.private_token(path, authority_uid=os.getuid())
            path.chmod(0o600)
            alias = Path(directory) / 'alias'
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                module.private_token(alias, authority_uid=os.getuid())
            alias.unlink()
            os.link(path, alias)
            with self.assertRaises(ValueError):
                module.private_token(path, authority_uid=os.getuid())

    def test_multiline_or_oversize_private_input_is_rejected_without_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'enrollment-token'
            for value in ('synthetic\nextra', 'x'*4097, ''):
                path.write_text(value)
                path.chmod(0o600)
                with self.subTest(size=len(value)), self.assertRaises(ValueError) as caught:
                    module.private_token(path, authority_uid=os.getuid())
                self.assertNotIn('synthetic', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
