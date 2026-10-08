import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

FILES = Path(__file__).parents[2] / 'roles/forgejo_runner/files'
sys.path.insert(0, str(FILES))
spec = importlib.util.spec_from_file_location('deployment_verify', FILES / 'verify.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class VerificationTests(unittest.TestCase):
    def test_read_only_verify_preserves_state_and_uses_only_systemctl_show(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'name': 'playbook', 'state_root': str(root),
                      'limits': {'memory_bytes': 1024, 'pids': 64, 'cpu_percent': 50}}
            calls = []
            def execute(argv):
                calls.append(argv)
                return 'LoadState=not-found\n'
            before = list(root.iterdir())
            result = module.observe(config, execute=execute, authority_uid=os.getuid(), process_reader=lambda: [])
            self.assertEqual('clean', result['phase'])
            self.assertTrue(result['runtime_absent'])
            self.assertEqual(before, list(root.iterdir()))
            self.assertEqual(1, len(calls))
            self.assertEqual(['systemctl', 'show'], calls[0][:2])

    def test_inactive_socket_is_not_contacted_or_activated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'worker.ext4').write_bytes(b'preserve')
            config = {'name': 'playbook', 'state_root': str(root), 'limits': {}}
            def execute(argv):
                self.assertEqual('show', argv[1])
                return 'LoadState=not-found\n'
            result = module.observe(config, execute=execute, authority_uid=os.getuid(), process_reader=lambda: [])
            self.assertFalse(result['runtime_absent'])
            self.assertEqual(b'preserve', (root / 'worker.ext4').read_bytes())

    def test_live_runtime_must_match_checkpoint_and_effective_limits(self):
        import lifecycle
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocation = {'device': 1, 'inode': 2, 'uid': 0, 'gid': 0, 'mode': 0o100600,
                          'nlink': 1, 'invocation': 'b'*32}
            state = {'phase': 'running', 'generation': 'a'*32, 'allocation': allocation,
                     'registration': {'id': 1, 'name': 'playbook-'+'a'*32, 'scope': 'user:example'},
                     'primary_error': None, 'cleanup_errors': []}
            with lifecycle.CheckpointStore(root, authority_uid=os.getuid()) as store:
                store.write(state)
            config = {'name': 'playbook', 'state_root': str(root),
                      'limits': {'memory_bytes': 1024, 'pids': 64, 'cpu_percent': 50}}
            observed = {'LoadState': 'loaded', 'ActiveState': 'active', 'InvocationID': 'b'*32,
                        'RootImage': str(root / 'runtime' / 'worker.ext4'),
                        'MemoryMax': '1024', 'MemorySwapMax': '0', 'TasksMax': '64',
                        'CPUQuotaPerSecUSec': '500ms', 'KillMode': 'control-group'}
            def execute(argv):
                return '\n'.join(k+'='+v for k,v in observed.items())
            result = module.observe(config, execute=execute, authority_uid=os.getuid(), process_reader=lambda: [])
            self.assertTrue(result['limits_verified'])
            observed['TasksMax'] = 'infinity'
            result = module.observe(config, execute=execute, authority_uid=os.getuid(), process_reader=lambda: [])
            self.assertFalse(result['limits_verified'])

    def test_surviving_worker_identity_prevents_empty_result(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'name': 'playbook', 'state_root': directory, 'limits': {},
                      'worker': {'uid': 2001, 'subuid_start': 1000000, 'subuid_count': 65536},
                      'controller': {'uid': 2002, 'subuid_start': 1065536, 'subuid_count': 65536}}
            result = module.observe(config, execute=lambda _: 'LoadState=not-found\n',
                authority_uid=os.getuid(), process_reader=lambda: [{'uid': 1000001}])
            self.assertFalse(result['runtime_absent'])
            self.assertEqual(1, result['owned_process_count'])


if __name__ == '__main__':
    unittest.main()
