import importlib.util
from pathlib import Path
import unittest
import tempfile
import json
import stat
import sys
from unittest.mock import patch

source = Path(__file__).parents[2] / 'roles/forgejo_runner/files/resources.py'
spec = importlib.util.spec_from_file_location('deployed_resources', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ResourceContractTests(unittest.TestCase):
    def test_persist_publishes_a_private_receipt_and_replaces_it_without_leftovers(self):
        expected = {'schema': 1, 'generation': 'a'*32,
                    'unit': 'forgejo-worker-playbook-'+'a'*32+'.service',
                    'invocation': None,
                    'resources': [{'path': '.', 'identity': [1, 3, 0, 0, 0o40700, 0],
                                   'parent_identity': [1, 4, 0, 0, 0o40700, 0]}]}
        with tempfile.TemporaryDirectory() as directory:
            runtime = module.OwnedRuntime(self.target(), directory, directory, {}, None, None)
            runtime.state_root = Path(directory).resolve()
            runtime.generation = 'a'*32
            # Supply trusted root-owned metadata without requiring local root.
            # Receipt validation and all filesystem publication remain real.
            with patch.object(runtime, 'receipt', return_value=expected):
                for invocation in (None, 'b'*32):
                    expected['invocation'] = invocation
                    try:
                        runtime.persist()
                    except TypeError as error:
                        self.fail(f'Receipt publication failed: {error}')
                    destination = runtime.state_root / 'resources.json'
                    self.assertEqual(expected, json.loads(destination.read_text()))
                    self.assertEqual(0o600, stat.S_IMODE(destination.stat().st_mode))
                    self.assertEqual([destination], list(runtime.state_root.iterdir()))

    def test_image_home_permits_trusted_setup_search_without_other_user_read_or_write(self):
        # The launcher is root without DAC override/search. Its root-owned
        # network-ready file is inside the worker's home, so Unix directory
        # search is required even though listing and writes are forbidden.
        class ImageDirectoriesCreated(Exception):
            pass

        from contextlib import ExitStack
        config = self.target()
        config['worker'].update(name='worker', subuid_start=200000,
                                subgid_start=200000, subuid_count=65536)
        config['controller'] = {'name': 'controller', 'uid': 2202, 'gid': 2202}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            manifest = {'artifacts': {name: {} for name in ('job.oci', 'forgejo-runner', 'netavark')}}
            runtime = module.OwnedRuntime(config, directory, directory, manifest, None, None)
            runtime.state_root = Path(directory)
            runtime.root = runtime.state_root / 'runtime'
            patches.enter_context(patch.object(module.os, 'geteuid', return_value=0))
            patches.enter_context(patch.object(module.os, 'chown'))
            patches.enter_context(patch.object(runtime, 'record'))
            patches.enter_context(patch.object(runtime, 'assert_empty'))
            patches.enter_context(patch.object(runtime, 'capacity'))
            patches.enter_context(patch('scripts.forgejo_runner.host_probe.secure_path', return_value=True))
            patches.enter_context(patch('scripts.forgejo_runner.deployment_assets.verify'))
            patches.enter_context(patch('scripts.forgejo_runner.deployment_assets.validate_executable'))
            patches.enter_context(patch.object(sys, 'path', [str(source.parent), *sys.path]))
            patches.enter_context(patch('pool.service_baseline', return_value=[]))
            patches.enter_context(patch.object(module.OwnedUnit, 'observe', return_value={'LoadState': 'not-found'}))
            # Package binaries and setcap need the Debian host. Keep directory
            # construction real and stop before the rest of the image is built.
            patches.enter_context(patch.object(module.shutil, 'copyfile'))
            def command(argv):
                if argv[0] == 'systemctl':
                    return 'running'
                if argv[0] == 'getcap':
                    capability = 'cap_setuid' if argv[1].endswith('newuidmap') else 'cap_setgid'
                    return argv[1] + ' ' + capability + '=ep'
                return ''
            patches.enter_context(patch.object(module, 'command', side_effect=command))
            patches.enter_context(patch.object(module, 'worker_files', side_effect=ImageDirectoriesCreated))
            # The real installation provides this public launcher input.
            (Path(directory) / 'worker_launch.py').write_text('synthetic launcher')
            with self.assertRaises(ImageDirectoriesCreated):
                runtime.prepare('a'*32)
            home = runtime.root / 'rootfs/work'
            mode = stat.S_IMODE(home.stat().st_mode)
            self.assertTrue(mode & stat.S_IXOTH, 'Trusted root cannot reach its network readiness file')
            self.assertEqual(0, mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IRGRP | stat.S_IWGRP))
            self.assertEqual(0o700, stat.S_IMODE((runtime.root / 'rootfs/controller').stat().st_mode))

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
