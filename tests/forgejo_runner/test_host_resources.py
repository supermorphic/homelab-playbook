"""Temporary host resources need fresh identities and independent failure results."""

import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_host_fixture import target_descriptor


class HostResourceTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('scripts.forgejo_runner.host_resources')
        except ModuleNotFoundError:
            self.fail('host resource ownership and cleanup are not implemented')

    def test_cleanup_rejects_changed_resource_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned = root / 'owned'
            owned.write_text('owned test resource')
            sentinel = root / 'unrelated'
            sentinel.write_text('preserve me')
            resources = self.module.Resources()
            resources.record_file(owned)
            owned.rename(root / 'original')
            owned.write_text('replacement belongs to someone else')
            errors = resources.cleanup()
            self.assertTrue(errors)
            self.assertEqual('replacement belongs to someone else', owned.read_text())
            self.assertEqual('preserve me', sentinel.read_text())

    def test_cleanup_removes_owned_files_but_preserves_unrelated_sentinel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned = root / 'owned'
            owned.write_text('owned')
            sentinel = root / 'unrelated'
            sentinel.write_text('preserve')
            resources = self.module.Resources()
            resources.record_file(owned)
            self.assertEqual([], resources.cleanup())
            self.assertFalse(owned.exists())
            self.assertEqual('preserve', sentinel.read_text())

    def test_primary_failure_preserved_when_cleanup_fails(self):
        def primary():
            raise RuntimeError('synthetic primary failure')
        result = self.module.run_with_cleanup(primary, lambda: ['synthetic cleanup failure'])
        self.assertEqual(1, result['exit_code'])
        self.assertEqual('RuntimeError', result['primary_error'])
        self.assertEqual(['synthetic cleanup failure'], result['cleanup_errors'])
        self.assertNotIn('synthetic primary failure', str(result))

    def test_cleanup_failure_fails_successful_probe(self):
        result = self.module.run_with_cleanup(lambda: {'disk_bounded': True}, lambda: ['changed identity'])
        self.assertEqual(1, result['exit_code'])
        self.assertEqual({'disk_bounded': True}, result['observations'])

    def test_host_command_failure_retains_bounded_private_diagnostic(self):
        import subprocess
        response = subprocess.CompletedProcess(['synthetic-tool'], 1, '', 'private startup failure')
        with patch.object(self.module.subprocess, 'run', return_value=response):
            try:
                self.module.command(['synthetic-tool'])
            except RuntimeError as error:
                self.assertNotIn('private startup failure', str(error))
                def primary():
                    raise error
                outcome = self.module.run_with_cleanup(primary, lambda: [])
                self.assertEqual('private startup failure', outcome.get('diagnostic'))
            else:
                self.fail('Failed host command was accepted')

    def test_cleanup_still_runs_after_partial_setup_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            owned = Path(directory) / 'owned'
            resources = self.module.Resources()
            def setup():
                owned.write_text('partial setup')
                resources.record_file(owned)
                raise ValueError('private detail')
            result = self.module.run_with_cleanup(setup, resources.cleanup)
            self.assertEqual(1, result['exit_code'])
            self.assertFalse(owned.exists())
            self.assertEqual([], result['cleanup_errors'])

    def test_cleanup_preserves_symlink_replacement_and_target(self):
        with tempfile.TemporaryDirectory() as directory:
            owned = Path(directory) / 'owned'
            owned.write_text('owned')
            sentinel = Path(directory) / 'sentinel'
            sentinel.write_text('unrelated')
            resources = self.module.Resources()
            resources.record_file(owned)
            owned.unlink()
            owned.symlink_to(sentinel)
            self.assertTrue(resources.cleanup())
            self.assertTrue(owned.is_symlink())
            self.assertEqual('unrelated', sentinel.read_text())

    def test_capacity_reserve_rejects_probe_before_setup(self):
        target = {'limits': {'memory_bytes': 134217728, 'disk_bytes': 134217728,
                            'pids': 32, 'cpu_percent': 25, 'job_seconds': 30}}
        capacity = {'memory_total': 8 * 1024**3, 'memory_available': 4 * 1024**3,
                    'disk_available': 10 * 1024**3}
        self.module.validate_probe_budget(target, capacity)
        for key, value in [('memory_available', 1024), ('disk_available', 1024)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.module.validate_probe_budget(target, {**capacity, key: value})
        for key, value in [('memory_bytes', 1024**3), ('disk_bytes', 1024**3),
                           ('pids', 1024), ('cpu_percent', 200), ('job_seconds', 3600)]:
            bad = {'limits': {**target['limits'], key: value}}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.module.validate_probe_budget(bad, capacity)

    def test_owned_nested_directories_are_removed_without_recursive_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            owned = Path(directory) / 'owned'
            owned.mkdir()
            resources = self.module.Resources()
            resources.record_file(owned)
            child = owned / 'child'
            child.mkdir()
            resources.record_file(child)
            payload = child / 'payload'
            payload.write_text('owned')
            resources.record_file(payload)
            self.assertEqual([], resources.cleanup())
            self.assertFalse(owned.exists())

    def test_unit_replacement_is_preserved(self):
        state = {'LoadState': 'loaded', 'InvocationID': 'original',
                 'RootImage': '/synthetic/image', 'ActiveState': 'active',
                 'SubState': 'running', 'ControlGroup': '/system.slice/synthetic.service'}
        def command(argv):
            if argv[1] == 'show':
                return '\n'.join(key + '=' + value for key, value in state.items())
            self.fail('A replaced unit must not be stopped')
        unit = self.module.OwnedUnit('synthetic.service', '/synthetic/image', command)
        unit.claim()
        state['InvocationID'] = 'replacement'
        self.assertTrue(unit.cleanup())
        self.assertEqual('active', state['ActiveState'])

    def test_unit_created_after_lost_start_response_is_reconciled(self):
        state = {'LoadState': 'loaded', 'InvocationID': 'owned',
                 'RootImage': '/synthetic/image', 'ActiveState': 'active',
                 'SubState': 'running', 'ControlGroup': ''}
        def command(argv):
            if argv[1] == 'show':
                return '\n'.join(key + '=' + value for key, value in state.items())
            if argv[1] == 'stop':
                state['ActiveState'] = 'inactive'
                state['SubState'] = 'dead'
                state['LoadState'] = 'not-found'
                return ''
            self.fail('Unexpected host operation')
        unit = self.module.OwnedUnit('synthetic.service', '/synthetic/image', command)
        unit.attempted = True
        self.assertEqual([], unit.cleanup())
        self.assertEqual('inactive', state['ActiveState'])

    def test_unknown_unit_from_failed_start_is_preserved(self):
        state = 'LoadState=loaded\nRootImage=/unrelated/image\nInvocationID=unrelated\nActiveState=active\n'
        def command(argv):
            if argv[1] == 'show':
                return state
            self.fail('Unrelated unit must not be stopped')
        unit = self.module.OwnedUnit('synthetic.service', '/synthetic/image', command)
        unit.attempted = True
        self.assertTrue(unit.cleanup())

    def test_failed_unit_metadata_is_retired_before_cleanup_succeeds(self):
        state = {'LoadState': 'loaded', 'InvocationID': 'owned', 'RootImage': '/synthetic/image',
                 'ActiveState': 'failed', 'SubState': 'failed', 'ControlGroup': ''}
        def command(argv):
            if argv[1] == 'show':
                return '\n'.join(key + '=' + value for key, value in state.items())
            if argv[1] == 'stop':
                return ''  # Real systemd retains failed state after stop.
            if argv[1] == 'reset-failed':
                self.assertEqual(['systemctl', 'reset-failed', '--', 'synthetic.service'], argv)
                state.update(LoadState='not-found', ActiveState='inactive')
                return ''
            self.fail('Unexpected host operation')
        unit = self.module.OwnedUnit('synthetic.service', '/synthetic/image', command)
        unit.claim()
        self.assertEqual([], unit.cleanup())
        self.assertEqual('not-found', state['LoadState'])

    def test_partial_resource_probe_does_not_open_full_host_acceptance(self):
        import json
        from scripts.forgejo_runner import host_fixture
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=root / '.tmp') as directory:
            target = target_descriptor()
            target['limits'].update(memory_bytes=134217728, disk_bytes=134217728,
                                    pids=32, cpu_percent=25, job_seconds=30)
            descriptor = Path(directory) / 'target.json'
            descriptor.write_text(json.dumps(target))
            with patch.object(host_fixture, 'inspect_resources', return_value={'exit_code': 0}) as execute:
                self.assertEqual(0, host_fixture.run(None, descriptor, resource_probe_only=True))
                self.assertEqual(1, execute.call_count)
                with self.assertRaises(RuntimeError):
                    host_fixture.run(None, descriptor)
                self.assertEqual(1, execute.call_count)

    def test_invalid_probe_budget_never_reaches_ssh(self):
        import json
        from scripts.forgejo_runner import host_fixture
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=root / '.tmp') as directory:
            descriptor = Path(directory) / 'target.json'
            descriptor.write_text(json.dumps(target_descriptor()))
            with patch('subprocess.run', side_effect=AssertionError('Unsafe budgets reached SSH')):
                with self.assertRaises(ValueError):
                    host_fixture.run(None, descriptor, resource_probe_only=True)

    def test_worker_refuses_exhaustion_when_effective_bounds_are_wrong(self):
        try:
            probe = importlib.import_module('scripts.forgejo_runner.resource_probe')
        except ModuleNotFoundError:
            self.fail('independent resource verification is missing')
        limits = {'memory_bytes': 134217728, 'pids': 32, 'cpu_percent': 25}
        readings = {'memory.max': '134217728', 'memory.swap.max': '0',
                    'pids.max': '32', 'cpu.max': '25000 100000'}
        self.assertTrue(probe.bounded_limits(readings, limits))
        for key, value in [('memory.max', 'max'), ('memory.max', '268435456'),
                           ('memory.swap.max', '134217728'), ('pids.max', 'max'),
                           ('pids.max', '64'), ('cpu.max', 'max 100000'),
                           ('cpu.max', '50000 100000')]:
            with self.subTest(key=key):
                self.assertFalse(probe.bounded_limits({**readings, key: value}, limits))

    def test_resource_stage_creates_bounded_unit_and_cleans_its_owned_image(self):
        self.check_resource_stage()

    def test_worker_stage_uses_supported_transient_cgroup_namespace_property(self):
        self.check_resource_stage(worker_stage=True)

    def test_worker_default_target_references_loadable_probe_unit(self):
        self.check_resource_stage(worker_stage=True, worker_unit=True)

    def test_worker_process_cleanup_failure_preserves_its_image(self):
        self.check_resource_stage(worker_stage=True, worker_cleanup_failure=True)

    def test_resource_stage_cleans_unit_created_before_start_response_was_lost(self):
        self.check_resource_stage(lost_start=True)

    def test_failed_launcher_keeps_private_diagnostics_and_retires_its_unit(self):
        self.check_resource_stage(launch_failed=True)

    def test_image_still_attached_to_kernel_is_preserved_after_unit_stop(self):
        self.check_resource_stage(image_busy=True)

    def test_existing_fixture_mount_capacity_is_checked_before_setup(self):
        self.check_resource_stage(small_destination=True)

    def test_destination_capacity_is_rechecked_immediately_before_allocation(self):
        self.check_resource_stage(destination_shrunk=True)

    def check_resource_stage(self, *, lost_start=False, image_busy=False,
                             small_destination=False, destination_shrunk=False, launch_failed=False,
                             worker_stage=False, worker_unit=False, worker_cleanup_failure=False):
        import json
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / 'fixture'
            sentinel = Path(directory) / 'unrelated'
            sentinel.write_text('preserve')
            if small_destination:
                parent.mkdir(mode=0o700)
                (parent / 'ownership').write_text('synthetic-57\n')
            target = target_descriptor()
            target['state_root'] = str(parent / 'synthetic-57')
            target['limits'].update(memory_bytes=134217728, disk_bytes=134217728,
                                    pids=32, cpu_percent=25, job_seconds=30)
            state = {'LoadState': 'not-found'}
            def command(argv):
                if argv[:2] == ['systemctl', 'show']:
                    return '\n'.join(key + '=' + value for key, value in state.items())
                if argv[:2] == ['systemctl', 'is-system-running']:
                    return 'running'
                if argv[:2] == ['systemctl', 'list-units']:
                    return 'unrelated.service loaded active running synthetic service'
                if argv[0] in ('fallocate', 'mkfs.ext4', 'setcap'):
                    return ''
                if argv[0] == 'getcap':
                    capability = 'cap_setuid' if argv[1].endswith('/newuidmap') else 'cap_setgid'
                    return argv[1] + ' ' + capability + '=ep\n'
                if argv[0] == 'systemd-run':
                    properties = dict(item.removeprefix('--property=').split('=', 1)
                                      for item in argv if item.startswith('--property='))
                    if worker_stage:
                        # systemd v257's transient API retains a boolean legacy
                        # property and exposes the namespace enum through Ex.
                        if properties.get('ProtectControlGroups', 'no') not in ('yes', 'no'):
                            raise RuntimeError('Transient property requires a boolean')
                        self.assertEqual('private', properties['ProtectControlGroupsEx'])
                        self.assertEqual('yes', properties['Delegate'])
                        self.assertEqual('no', properties['NoNewPrivileges'])
                        self.assertEqual('yes', properties['PrivatePIDs'])
                        self.assertEqual('default', properties['ProtectProc'])
                        for helper in ('newuidmap', 'newgidmap'):
                            copy = Path(properties['RootImage']).parent / helper
                            self.assertTrue(copy.is_file(), 'Worker receives only an owned helper copy')
                            self.assertEqual(0, copy.stat().st_mode & 0o6000)
                            self.assertIn(str(copy) + ':/usr/bin/' + helper, properties['BindReadOnlyPaths'])
                    for key, value in {'MemoryMax': '134217728', 'MemorySwapMax': '0',
                                       'TasksMax': '32', 'CPUQuota': '25%', 'RuntimeMaxSec': '30s',
                                       'PrivateNetwork': 'yes', 'ProtectControlGroups': 'yes',
                                       'KillMode': 'control-group', 'BindReadOnlyPaths': '/usr',
                                       'ReadWritePaths': '/work', 'BindLogSockets': 'no',
                                       'User': '0', 'Group': '0', 'NoNewPrivileges': 'yes',
                                       'AmbientCapabilities': 'CAP_SETUID',
                                       'CapabilityBoundingSet': 'CAP_SETUID CAP_SETGID CAP_SETPCAP'}.items():
                        if worker_stage and key in ('ProtectControlGroups', 'BindReadOnlyPaths', 'NoNewPrivileges',
                                                     'AmbientCapabilities', 'CapabilityBoundingSet'):
                            continue
                        self.assertEqual(value, properties[key])
                    if not worker_stage:
                        program = argv.index('/usr/bin/setpriv')
                        self.assertEqual(['/usr/bin/setpriv', '--reuid=2202', '--regid=2202',
                                      '--clear-groups', '--bounding-set=-all', '--inh-caps=-all',
                                      '--ambient-caps=-all', '/usr/bin/python3', '/probe.py'],
                                     argv[program:-1])
                    image = Path(properties['RootImage'])
                    self.assertTrue(image.is_file())
                    if worker_unit:
                        import configparser
                        units = image.parent / 'rootfs/etc/systemd/user'
                        canonical = units / 'worker-probe.service'
                        self.assertTrue(canonical.is_file(), 'The user manager must find the actual unit')
                        dependency = units / 'default.target.wants/worker-probe.service'
                        self.assertTrue(dependency.is_symlink())
                        self.assertEqual(canonical.resolve(), dependency.resolve(strict=True))
                        configuration = configparser.ConfigParser()
                        configuration.read(units.parent / 'user.conf')
                        self.assertEqual('infinity', configuration['Manager']['DefaultTasksMax'])
                    receipt = image.parent / 'receipt.json'
                    self.assertTrue(receipt.is_file(), 'Ownership must survive an interrupted controller')
                    journal = json.loads(receipt.read_text())
                    self.assertEqual('forgejo-' + ('worker-' if worker_stage else 'resource-')
                                     + 'synthetic-57.service', journal['unit'])
                    self.assertEqual(str(image), journal['image'])
                    self.assertEqual(0, receipt.stat().st_mode & 0o077)
                    error_log = Path(properties['StandardError'].removeprefix('file:'))
                    self.assertTrue(error_log.is_file())
                    self.assertEqual(image.parent, error_log.parent)
                    self.assertEqual(0, error_log.stat().st_mode & 0o077)
                    state.update(LoadState='loaded', InvocationID='synthetic-owned',
                                 RootImage=str(image), ActiveState='active',
                                 SubState='exited', ControlGroup='')
                    Path(properties['StandardOutput'].removeprefix('file:')).write_text(json.dumps({
                        'limits_verified': True, 'private_loopback': True,
                        'disk_bounded': True, 'pids_bounded': True}))
                    if lost_start:
                        raise RuntimeError('synthetic private startup diagnostic')
                    if launch_failed:
                        state.update(ActiveState='failed', SubState='failed')
                        error_log.write_text('synthetic launcher diagnostic')
                        raise RuntimeError('Synthetic launcher failed')
                    return ''
                if argv[:2] == ['systemctl', 'stop']:
                    if state['ActiveState'] != 'failed':
                        state.update(ActiveState='inactive', LoadState='not-found')
                    return ''
                if argv[:2] == ['systemctl', 'reset-failed']:
                    state.update(ActiveState='inactive', LoadState='not-found')
                    return ''
                self.fail('Unexpected host operation')
            actual_stat = self.module.os.stat
            def stat(path, *args, **kwargs):
                if str(path) == '/proc/self/ns/net':
                    return SimpleNamespace(st_ino=7)
                return actual_stat(path, *args, **kwargs)
            def capacity(path):
                free = 10 * 1024**3
                if small_destination and Path(path) == parent:
                    free = 1024
                if destination_shrunk and Path(path) == Path(target['state_root']):
                    free = 1024
                return {'memory_total': 8 * 1024**3, 'memory_available': 4 * 1024**3,
                        'disk_available': free}
            def copy_helper(source, destination):
                self.assertIn(source, ('/usr/bin/newuidmap', '/usr/bin/newgidmap'))
                Path(destination).write_bytes(b'synthetic stock helper')
            with patch.object(self.module, 'command', side_effect=command), \
                 patch.object(self.module, 'host_capacity', side_effect=capacity), \
                 patch('os.geteuid', return_value=0), patch('os.chown'), \
                 patch('os.stat', side_effect=stat), patch('shutil.which', return_value='/synthetic/tool'), \
                 patch.object(self.module.shutil, 'copyfile', side_effect=copy_helper), \
                 patch.object(self.module, 'image_attached', return_value=image_busy):
                if worker_stage:
                    from scripts.forgejo_runner.host_worker import worker_files
                    extra = (worker_files(target, 'synthetic trusted launcher') if worker_unit
                             else {'worker-launch.py': 'synthetic trusted launcher'})
                    def after_stop(target):
                        self.assertEqual('inactive', state['ActiveState'])
                        self.assertTrue((Path(target['state_root']) / 'worker.ext4').is_file())
                        return ['Synthetic worker process survived'] if worker_cleanup_failure else []
                    result = self.module.run_image_probe(target, lambda _: {}, lambda *a, **k: None,
                        'synthetic trusted probe', stage='worker-only',
                        extra_files=extra,
                        worker_observer=lambda t, observed, unit: observed, after_stop=after_stop)
                else:
                    result = self.module.run_resource_probe(target, lambda _: {}, lambda *a, **k: None,
                                                            'synthetic trusted probe')
            failed = lost_start or image_busy or small_destination or destination_shrunk or launch_failed or worker_cleanup_failure
            self.assertEqual(1 if failed else 0, result['exit_code'], result)
            self.assertNotIn('synthetic private startup diagnostic', str(result))
            self.assertEqual(image_busy or small_destination or worker_cleanup_failure, parent.exists())
            if small_destination or destination_shrunk:
                self.assertEqual('not-found', state['LoadState'], 'Unsafe capacity must prevent unit startup')
            else:
                self.assertEqual('inactive', state['ActiveState'])
            if launch_failed:
                self.assertEqual('synthetic launcher diagnostic', result['diagnostic'])
                self.assertEqual('not-found', state['LoadState'])
            self.assertEqual('preserve', sentinel.read_text())

    def test_registered_failure_preserves_primary_and_cleanup_outcomes(self):
        import contextlib
        import io
        import json
        from scripts.forgejo_runner import host_fixture
        root = Path(__file__).resolve().parents[2]
        for cleanup_errors in ([], ['Synthetic cleanup failure']):
            with self.subTest(cleanup_errors=cleanup_errors), tempfile.TemporaryDirectory(dir=root / '.tmp') as directory:
                target = target_descriptor()
                target['limits'].update(memory_bytes=134217728, disk_bytes=134217728,
                                        pids=32, cpu_percent=25, job_seconds=30)
                descriptor = Path(directory) / 'target.json'
                descriptor.write_text(json.dumps(target))
                outcome = {'exit_code': 1, 'primary_error': 'ValueError',
                           'cleanup_errors': cleanup_errors, 'acceptance': 'resources-only'}
                with patch.object(host_fixture, 'inspect_resources', return_value=outcome), \
                     contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError) as caught:
                    host_fixture.run(None, descriptor, resource_probe_only=True)
                evidence = list(Path(directory).glob('host-resource-outcome-*.json'))
                self.assertEqual(1, len(evidence), 'Registered failures must retain their separate outcomes')
                self.assertEqual(outcome, json.loads(evidence[0].read_text()))
                self.assertEqual(0, evidence[0].stat().st_mode & 0o077)
                if not cleanup_errors:
                    self.assertNotIn('retained allocation', str(caught.exception))

    def test_probe_requires_dropped_identity_and_all_privilege_sets(self):
        from scripts.forgejo_runner import resource_probe
        status = {'Uid': '2202 2202 2202 2202', 'Gid': '2202 2202 2202 2202', 'Groups': '',
                  'CapInh': '0000000000000000', 'CapPrm': '0000000000000000',
                  'CapEff': '0000000000000000', 'CapBnd': '0000000000000000',
                  'CapAmb': '0000000000000000', 'NoNewPrivs': '1'}
        self.assertTrue(resource_probe.unprivileged_worker(status, 2202, 2202))
        for key, value in [('Uid', '0 0 0 0'), ('Uid', '2202 0 2202 2202'),
                           ('Gid', '0 0 0 0'), ('Groups', '0'), ('NoNewPrivs', '0'),
                           ('CapInh', '80'), ('CapPrm', '80'), ('CapEff', '80'),
                           ('CapBnd', '80'), ('CapAmb', '80')]:
            with self.subTest(key=key):
                self.assertFalse(resource_probe.unprivileged_worker({**status, key: value}, 2202, 2202))


if __name__ == '__main__':
    unittest.main()
