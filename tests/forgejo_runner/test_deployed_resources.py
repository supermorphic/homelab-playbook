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

    def test_image_startup_inputs_remain_readable_under_private_supervisor_umask(self):
        from contextlib import ExitStack
        import os
        import shutil

        config = self.target()
        config['worker'].update(name='worker', subuid_start=200000,
                                subgid_start=200000, subuid_count=65536)
        config['controller'] = {'name': 'controller', 'uid': 2202, 'gid': 2202}
        artifacts = ('job.oci', 'forgejo-runner', 'netavark')
        manifest = {'artifacts': {name: {} for name in artifacts},
                    'job_image_id': 'a'*64, 'registry_images': []}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            root = Path(directory)
            installation, assets, state = [root / name for name in ('installation', 'assets', 'state')]
            for parent in (installation, assets, state):
                parent.mkdir(mode=0o700)
            script_files = ('worker_launch.py', 'worker_probe.py', 'native_tools.py',
                            'host_network.py', 'host_egress.py', 'network_setup.py', 'gateway_launch.py')
            for name in script_files:
                shutil.copyfile(source.parents[3] / 'scripts/forgejo_runner' / name, installation / name)
            for name in ('worker_start.py', 'controller_launch.py', 'network_start.py'):
                shutil.copyfile(source.parent / name, installation / name)
            for name in artifacts:
                (assets / name).write_bytes(b'synthetic public artifact')
            runtime = module.OwnedRuntime(config, installation, assets, manifest, None, None)
            runtime.state_root, runtime.root = state, state / 'runtime'
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
            # Package binaries, root-only copy authority and formatting require
            # Debian. Keep all image files, directories and symlinks real.
            copyfile = shutil.copyfile
            def copy_public_file(origin, destination):
                mode = stat.S_IMODE(destination.stat().st_mode)
                destination.chmod(mode | stat.S_IWUSR)
                try:
                    if str(origin) in ('/usr/bin/newuidmap', '/usr/bin/newgidmap'):
                        destination.write_bytes(b'synthetic mapping helper')
                    else:
                        copyfile(origin, destination)
                finally:
                    destination.chmod(mode)
            patches.enter_context(patch.object(module.shutil, 'copyfile', side_effect=copy_public_file))
            def command(argv):
                if argv[0] == 'systemctl':
                    return 'running'
                if argv[0] == 'getcap':
                    capability = 'cap_setuid' if argv[1].endswith('newuidmap') else 'cap_setgid'
                    return argv[1] + ' ' + capability + '=ep'
                return ''
            patches.enter_context(patch.object(module, 'command', side_effect=command))
            patches.enter_context(patch.object(module, 'observe_configuration', return_value={
                'forgejo_addresses': ['192.0.2.20'], 'host_addresses': ['192.0.2.10'],
                'dns_address': '192.0.2.53'}))
            read_text = Path.read_text
            def public_text(path, *args, **kwargs):
                return 'synthetic trust bundle' if str(path) == '/etc/ssl/certs/ca-certificates.crt' else read_text(path, *args, **kwargs)
            patches.enter_context(patch.object(Path, 'read_text', public_text))
            from types import SimpleNamespace
            real_stat = os.stat
            def image_stat(path, *args, **kwargs):
                return SimpleNamespace(st_ino=4242) if str(path) == '/proc/self/ns/net' else real_stat(path, *args, **kwargs)
            patches.enter_context(patch.object(module.os, 'stat', side_effect=image_stat))
            old_umask = os.umask(0o077)
            try:
                runtime.prepare('a'*32)
            finally:
                os.umask(old_umask)
            image = runtime.root / 'rootfs'
            startup_files = ('worker_launch.py', 'worker_start.py', 'worker_probe.py',
                             'controller_launch.py', 'native_tools.py', 'network_setup.py',
                             'trusted_commands.py', 'gateway-launch.py', 'worker.json',
                             'etc/passwd', 'etc/group', 'etc/subuid', 'etc/subgid',
                             'etc/systemd/user.conf', 'etc/systemd/user/worker-probe.service',
                             'etc/systemd/user/default.target.wants/worker-probe.service',
                             'etc/containers/storage.conf', 'etc/containers/containers.conf',
                             'etc/containers/policy.json', 'etc/runner-tools/netavark',
                             'etc/runner-tools/forgejo-runner', 'work/input/job.oci')
            for relative in startup_files:
                path = image / relative
                with self.subTest(startup=relative):
                    self.assertTrue(path.stat().st_mode & stat.S_IROTH, 'Worker cannot read startup input')
                    for parent in (path.parent, *path.parent.parents):
                        if not parent.is_relative_to(image):
                            break
                        # /work belongs to the worker; other ancestors belong
                        # to trusted root. Neither needs permission bypass.
                        self.assertTrue(parent.stat().st_mode & stat.S_IXOTH,
                                        f'Worker cannot search image ancestor {parent.relative_to(image)}')
            home_mode = stat.S_IMODE((image / 'work').stat().st_mode)
            self.assertTrue(home_mode & stat.S_IXOTH, 'Trusted root cannot reach its readiness file')
            self.assertEqual(0, home_mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IRGRP | stat.S_IWGRP))
            for private in ('controller', 'work/run', 'work/image-tmp', 'work/job-workspace'):
                self.assertEqual(0o700, stat.S_IMODE((image / private).stat().st_mode))

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
