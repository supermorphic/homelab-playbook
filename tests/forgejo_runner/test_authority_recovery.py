import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2] / 'roles/forgejo_runner/files'))
import control
import lifecycle


class AuthorityRecoveryTests(unittest.TestCase):
    def test_replacement_authority_can_unlock_registration_reconciliation_after_local_disposal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slot = {'name': 'playbook', 'repository': 'example/project', 'state_root': str(root)}
            def store(path):
                return lifecycle.CheckpointStore(path, authority_uid=os.getuid())
            with store(root) as checkpoint:
                checkpoint.write({**lifecycle.clean_checkpoint(), 'phase': 'quarantined', 'generation': 'a'*32})
            with patch.object(control, 'CheckpointStore', store), \
                    patch.object(control, 'execute', return_value='inactive'), \
                    patch.object(control, 'observe', return_value={'runtime_absent': True}):
                result = control.restore_authority({'slot': slot}, 'synthetic-replacement\n', 'example/project')
            self.assertEqual({'authority_restored': True, 'admission_stopped': True}, result)
            self.assertEqual('synthetic-replacement\n', (root / 'enrollment-token').read_text())
            self.assertEqual(0o600, (root / 'enrollment-token').stat().st_mode & 0o777)
            with store(root) as checkpoint:
                self.assertEqual('quarantined', checkpoint.read()['phase'])

    def test_restoration_rejects_wrong_repository_or_surviving_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slot = {'name': 'playbook', 'repository': 'example/project', 'state_root': str(root)}
            def store(path):
                return lifecycle.CheckpointStore(path, authority_uid=os.getuid())
            with patch.object(control, 'CheckpointStore', store), \
                    patch.object(control, 'execute', return_value='inactive'), \
                    patch.object(control, 'observe', return_value={'runtime_absent': False}):
                for repository in ('example/other', 'example/project'):
                    with self.subTest(repository=repository), self.assertRaises(ValueError):
                        control.restore_authority({'slot': slot}, 'synthetic-replacement', repository)
            self.assertFalse((root / 'enrollment-token').exists())
