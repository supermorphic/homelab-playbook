import importlib.util
from pathlib import Path
import unittest
import tempfile

source = Path(__file__).parents[2] / 'roles/forgejo_runner/files/resources.py'
spec = importlib.util.spec_from_file_location('deployed_resources', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ResourceContractTests(unittest.TestCase):
    def test_interrupted_removal_resumes_only_after_proving_absence(self):
        from scripts.forgejo_runner.host_resources import Resources
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / 'owned'
            root.mkdir()
            file = root / 'image'
            file.write_text('public owned payload')
            records = Resources()
            records.record_file(root)
            records.record_file(file)
            file.unlink()
            remaining = module.remaining_resources(records.entries)
            self.assertEqual([records.entries[0]], remaining)
            root.rmdir()
            self.assertEqual([], module.remaining_resources(records.entries))

    def test_a_replacement_parent_is_preserved_during_recovery(self):
        from scripts.forgejo_runner.host_resources import Resources
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / 'owned'
            root.mkdir()
            file = root / 'image'
            file.write_text('public owned payload')
            records = Resources()
            records.record_file(root)
            records.record_file(file)
            root.rename(parent / 'retained-original')
            root.mkdir()
            sentinel = root / 'unrelated'
            sentinel.write_text('preserve')
            with self.assertRaises(ValueError):
                module.remaining_resources(records.entries)
            self.assertEqual('preserve', sentinel.read_text())

    def target(self):
        return {'name': 'playbook', 'state_root': '/var/lib/forgejo-runner/playbook',
                'worker': {'uid': 2201, 'gid': 2201},
                'limits': {'memory_bytes': 2147483648, 'disk_bytes': 25769803776,
                           'pids': 768, 'cpu_percent': 200, 'job_seconds': 10800}}

    def test_outer_unit_bounds_controller_worker_and_siblings(self):
        properties = module.unit_properties(self.target(), '/var/lib/forgejo-runner/playbook/runtime/worker.ext4', [])
        for setting in ('MemoryMax=2147483648', 'MemorySwapMax=0', 'TasksMax=768',
                        'CPUQuota=200%', 'RuntimeMaxSec=10800s', 'KillMode=control-group',
                        'PrivateNetwork=yes', 'PrivatePIDs=yes', 'ProtectControlGroupsEx=private',
                        'RootImageOptions=nodev,nosuid'):
            self.assertIn(setting, properties)
        self.assertIn('ReadWritePaths=/work /controller', properties)
        self.assertFalse(any(setting == 'BindPaths=/' for setting in properties))

    def test_registry_policy_admits_only_the_declared_tested_images(self):
        reference = 'docker.io/library/postgres:17.11@sha256:' + 'a'*64
        policy = module.image_policy({'registry_images': [reference, 'docker.io/library/debian:13']})
        self.assertEqual([{'type': 'reject'}], policy['default'])
        self.assertEqual({'docker.io/library/postgres@sha256:'+'a'*64, 'docker.io/library/debian:13'},
                         set(policy['transports']['docker']))
        self.assertEqual({'/work/input/job.oci'}, set(policy['transports']['oci-archive']))
        for bad in ('https://registry.example/image', 'docker.io/image --privileged', '../../other'):
            with self.subTest(reference=bad), self.assertRaises(ValueError):
                module.image_policy({'registry_images': [bad]})

    def test_receipt_paths_cannot_select_other_slots_or_follow_job_paths(self):
        root = Path('/var/lib/forgejo-runner/playbook/runtime')
        identity = [1, 2, 0, 0, 0o100600, 1]
        valid = {'schema': 1, 'generation': 'a'*32,
                 'unit': 'forgejo-worker-playbook-'+'a'*32+'.service',
                 'invocation': 'b'*32,
                 'resources': [{'path': '.', 'identity': [1, 3, 0, 0, 0o40700, 0], 'parent_identity': [1, 4, 0, 0, 0o40700, 0]},
                               {'path': 'worker.ext4', 'identity': identity, 'parent_identity': [1, 3, 0, 0, 0o40700, 0]}]}
        module.validate_receipt(self.target(), 'a'*32, valid)
        import copy
        for path in ('../talos/worker.ext4', '/var/lib/other', 'rootfs/../../other'):
            invalid = copy.deepcopy(valid)
            invalid['resources'][-1]['path'] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                module.validate_receipt(self.target(), 'a'*32, invalid)
        invalid = copy.deepcopy(valid)
        invalid['unit'] = 'unrelated.service'
        with self.assertRaises(ValueError):
            module.validate_receipt(self.target(), 'a'*32, invalid)


if __name__ == '__main__':
    unittest.main()
