"""Supervisor checkpoints must remain private, exclusive and recoverable."""

import copy
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest


class CheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[2] / 'roles/forgejo_runner/files/lifecycle.py'
        spec = importlib.util.spec_from_file_location('runner_lifecycle', source)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def state(self):
        return {'phase': 'running', 'generation': 'a'*32,
                'allocation': {'device': 1, 'inode': 2, 'uid': 0, 'gid': 0,
                               'mode': 0o100600, 'nlink': 1, 'invocation': 'b'*32},
                'registration': {'id': 7, 'name': 'playbook-'+'a'*32,
                                 'repository': 'supermorphic/homelab-playbook'},
                'primary_error': None, 'cleanup_errors': []}

    def store(self, root):
        return self.module.CheckpointStore(root, authority_uid=os.getuid())

    def test_checkpoints_survive_reopening_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desired = self.state()
            with self.store(root) as store:
                self.assertEqual('clean', store.read()['phase'])
                store.write(desired)
            with self.store(root) as store:
                self.assertEqual(desired, store.read())
            self.assertEqual(0o600, stat.S_IMODE((root/'state.json').stat().st_mode))

    def test_a_second_admission_cannot_acquire_the_same_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.store(Path(directory)):
                with self.assertRaises(BlockingIOError):
                    with self.store(Path(directory)):
                        self.fail('A second admission acquired the slot')
            with self.store(Path(directory)):
                pass

    def test_quarantine_keeps_both_primary_and_cleanup_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = self.state()
            expected.update(phase='quarantined', primary_error='TimeoutExpired',
                            cleanup_errors=['OwnershipError'])
            with self.store(Path(directory)) as store:
                store.write(expected)
                self.assertEqual(expected, store.read())

    def test_rejected_state_does_not_replace_the_last_valid_checkpoint(self):
        for kind in ('token', 'path', 'phase', 'incomplete', 'error_text'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                desired = self.state()
                invalid = copy.deepcopy(desired)
                if kind == 'token':
                    invalid['registration']['token'] = 'synthetic-private-value'
                elif kind == 'path':
                    invalid['allocation']['path'] = '/unrelated'
                elif kind == 'phase':
                    invalid['phase'] = 'unexpected'
                elif kind == 'incomplete':
                    invalid['registration'] = None
                else:
                    invalid['primary_error'] = 'Error includes synthetic private text'
                with self.store(Path(directory)) as store:
                    store.write(desired)
                    with self.assertRaises(ValueError):
                        store.write(invalid)
                    self.assertEqual(desired, store.read())

    def test_state_links_are_preserved_and_rejected(self):
        for hard in (False, True):
            with self.subTest(hard=hard), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                other = root/'unrelated'
                other.write_text('preserve')
                other.chmod(0o600)
                if hard:
                    os.link(other, root/'state.json')
                else:
                    (root/'state.json').symlink_to(other)
                with self.store(root) as store:
                    with self.assertRaises((ValueError, OSError)):
                        store.read()
                    with self.assertRaises((ValueError, OSError)):
                        store.write(self.state())
                self.assertEqual('preserve', other.read_text())

    def test_an_unsafe_state_directory_is_rejected_before_creating_a_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            with self.assertRaises(ValueError):
                with self.store(root):
                    pass
            self.assertEqual([], list(root.iterdir()))

    def test_malformed_state_is_reported_without_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'state.json'
            path.write_text('{partial')
            path.chmod(0o600)
            with self.store(root) as store:
                with self.assertRaises(ValueError):
                    store.read()
            self.assertEqual('{partial', path.read_text())

    def test_non_string_phase_is_rejected_without_replacing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desired = self.state()
            with self.store(root) as store:
                store.write(desired)
                for phase in ([], {}, None, 1):
                    invalid = {**desired, 'phase': phase}
                    with self.subTest(phase=phase), self.assertRaises(ValueError):
                        store.write(invalid)
                    self.assertEqual(desired, store.read())

    def test_observational_read_creates_no_state_or_admission_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = list(root.iterdir())
            self.assertEqual('clean', self.module.read_checkpoint(root, authority_uid=os.getuid())['phase'])
            self.assertEqual(before, list(root.iterdir()))


if __name__ == '__main__':
    unittest.main()
