"""The worker feasibility stage must keep the host boundary independently bounded."""

import importlib
import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from test_host_fixture import target_descriptor


class HostWorkerTests(unittest.TestCase):
    def setUp(self):
        try:
            self.worker = importlib.import_module('scripts.forgejo_runner.host_worker')
        except ModuleNotFoundError:
            self.fail('The bounded rootless worker experiment is missing')

    def target(self):
        target = target_descriptor()
        target['limits'].update(memory_bytes=536870912, disk_bytes=268435456,
                                pids=128, cpu_percent=50, job_seconds=90)
        return target

    def test_worker_budget_rejects_large_jobs_before_setup(self):
        target = self.target()
        self.worker.validate_worker_budget(target)
        for key, value in [('memory_bytes', 2147483648), ('disk_bytes', 8589934592),
                           ('pids', 512), ('cpu_percent', 100), ('job_seconds', 7200)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.worker.validate_worker_budget({**target, 'limits': {**target['limits'], key: value}})

    def test_worker_budget_preserves_service_reserve(self):
        target = self.target()
        capacity = {'memory_total': 8589934592, 'memory_available': 4294967296,
                    'disk_available': 4294967296}
        self.worker.validate_worker_budget(target, capacity)
        for key, value in [('memory_available', 1073741824),
                           ('disk_available', 1073741824), ('memory_total', 1073741824)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.worker.validate_worker_budget(target, {**capacity, key: value})

    def test_process_outside_outer_cgroup_cannot_count_as_acceptance(self):
        target = self.target()
        root = '/system.slice/forgejo-worker-synthetic.service'
        records = [{'uid': 2202, 'gid': 2202, 'cgroup': root + '/probe.service',
                    'net': 456, 'userns': 12, 'pidns': 456, 'effective_caps': 0},
                   {'uid': 1065536, 'gid': 1065536, 'cgroup': root + '/libpod.scope',
                    'net': 456, 'userns': 34, 'pidns': 456, 'effective_caps': 0}]
        self.worker.validate_process_boundary(target, records, root, 123, 12, 123)
        for replacement in ({'cgroup': root + '-other/probe.service'}, {'net': 123},
                             {'effective_caps': 2097152, 'userns': 12}, {'pidns': 123}):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                self.worker.validate_process_boundary(target, [records[0], {**records[1], **replacement}], root, 123, 12, 123)

    def test_boundary_observation_requires_both_worker_and_subordinate_processes(self):
        root = '/system.slice/forgejo-worker-synthetic.service'
        with self.assertRaises(ValueError):
            self.worker.validate_process_boundary(self.target(), [
                {'uid': 2202, 'gid': 2202, 'cgroup': root + '/probe.service',
                 'net': 456, 'userns': 12, 'pidns': 456, 'effective_caps': 0}], root, 123, 12, 123)

    def test_mapped_container_capabilities_are_confined_to_its_user_namespace(self):
        root = '/system.slice/forgejo-worker-synthetic.service'
        self.worker.validate_process_boundary(self.target(), [
            {'uid': 2202, 'gid': 2202, 'cgroup': root + '/probe.service',
             'net': 456, 'userns': 12, 'pidns': 456, 'effective_caps': 0},
            {'uid': 1065536, 'gid': 1065536, 'cgroup': root + '/container.scope',
             'net': 456, 'userns': 34, 'pidns': 456, 'effective_caps': 2097152}], root, 123, 12, 123)

    def test_missing_worker_evidence_fails_closed(self):
        from scripts.forgejo_runner import host_fixture
        for observations in ({}, {'rootless_api': True}, {'rootless_api': False}):
            outcome = {'exit_code': 0, 'primary_error': None, 'cleanup_errors': [],
                       'acceptance': 'worker-only', 'observations': observations}
            with patch.object(host_fixture, 'ssh_observation', return_value=outcome):
                with self.assertRaises(ValueError):
                    host_fixture.inspect_workers(self.target())

    def test_worker_acceptance_requires_published_loopback_http(self):
        from scripts.forgejo_runner import host_fixture
        outcomes = {name: True for name in ('rootless_api', 'sibling_containers', 'mapped_bind',
            'private_loopback', 'user_manager', 'bounded_storage', 'outer_limits', 'process_boundary')}
        for result in (outcomes, {**outcomes, 'published_loopback': False}):
            outcome = {'exit_code': 0, 'primary_error': None, 'cleanup_errors': [],
                       'acceptance': 'worker-only', 'observations': result}
            with patch.object(host_fixture, 'ssh_observation', return_value=outcome):
                with self.assertRaises(ValueError):
                    host_fixture.inspect_workers(self.target())
        outcome['observations'] = {**outcomes, 'published_loopback': True}
        with patch.object(host_fixture, 'ssh_observation', return_value=outcome):
            self.assertEqual(outcome, host_fixture.inspect_workers(self.target()))

    def test_published_port_rejects_non_loopback_or_ambiguous_bindings(self):
        from scripts.forgejo_runner import worker_probe
        parser = getattr(worker_probe, 'published_port', None)
        self.assertTrue(callable(parser), 'Published-port validation is missing')
        self.assertEqual(49152, parser({'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49152'}]}))
        for bindings in ({}, {'8080/tcp': []},
                {'8080/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '49152'}]},
                {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '0'}]},
                {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '65536'}]},
                {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '80'}]},
                {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '49152'}] * 2}):
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                parser(bindings)

    def test_mapped_runtime_exec_preserves_namespace_capabilities(self):
        from scripts.forgejo_runner import worker_probe
        validator = getattr(worker_probe, 'validate_namespace_credentials', None)
        self.assertTrue(callable(validator), 'Mapped runtime credential observation is missing')
        status = {'Uid': '0 0 0 0', 'Gid': '0 0 0 0',
                  'CapEff': '1ffffffffff', 'CapPrm': '1ffffffffff', 'CapBnd': '1ffffffffff'}
        validator(status, 40)
        for key, value in (('Uid', '1 1 1 1'), ('Gid', '1 1 1 1'),
                           ('CapEff', 'c0'), ('CapPrm', 'c0'), ('CapBnd', 'c0')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validator({**status, key: value}, 40)

    def test_published_http_uses_actual_loopback_response(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        from scripts.forgejo_runner import worker_probe
        checker = getattr(worker_probe, 'check_published_http', None)
        self.assertTrue(callable(checker), 'Published-port HTTP observation is missing')
        class Handler(BaseHTTPRequestHandler):
            payload = b'{"fixture":"published-loopback"}'
            response = 200
            def do_GET(self):
                self.send_response(self.response); self.end_headers(); self.wfile.write(self.payload)
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        bindings = {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(server.server_port)}]}
        try:
            checker(bindings)
            for status, payload in ((503, b'{"fixture":"published-loopback"}'),
                                    (200, b'{"fixture":"unrelated"}'), (200, b'x' * 1025)):
                Handler.response, Handler.payload = status, payload
                with self.subTest(status=status, payload_size=len(payload)), self.assertRaises(ValueError):
                    checker(bindings)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_worker_credentials_leave_only_mapping_helper_bounding_capabilities(self):
        try:
            probe = importlib.import_module('scripts.forgejo_runner.worker_probe')
        except ModuleNotFoundError:
            self.fail('Independent worker credential checks are missing')
        readings = {'Uid': '2202 2202 2202 2202', 'Gid': '2202 2202 2202 2202',
                    'Groups': '', 'CapEff': '0', 'CapPrm': '0', 'CapInh': '0',
                    'CapAmb': '0', 'CapBnd': 'c0', 'NoNewPrivs': '0'}
        probe.validate_credentials(readings, 2202, 2202)
        for key, value in [('CapEff', '200000'), ('CapBnd', '2000c0'),
                           ('Uid', '0 0 0 0'), ('Groups', '0'), ('NoNewPrivs', '1')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                probe.validate_credentials({**readings, key: value}, 2202, 2202)

    def test_worker_result_reader_does_not_follow_worker_symlinks(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / 'proc/4321'; process.mkdir(parents=True)
            image = root / 'image'; (image / 'work').mkdir(parents=True)
            (process / 'root').symlink_to(image)
            (process / 'cgroup').write_text('0::/system.slice/owned.service/init.scope\n')
            target = self.target(); target['worker'].update(uid=os.getuid(), gid=os.getgid())
            unit = {'MainPID': '4321', 'ControlGroup': '/system.slice/owned.service'}
            result = image / 'work/worker-result.json'
            result.write_text('{"synthetic":true}')
            with patch.object(self.worker, 'validate_runtime_storage', create=True) as runtime_check:
                self.assertEqual({'synthetic': True}, self.worker.read_worker_result(target, unit, root / 'proc')['observations'])
                runtime_check.assert_called_once()
            result.unlink()
            outside = root / 'unrelated'; outside.write_text('private unrelated sentinel')
            result.symlink_to(outside)
            with self.assertRaises((OSError, ValueError)):
                self.worker.read_worker_result(target, unit, root / 'proc')
            self.assertEqual('private unrelated sentinel', outside.read_text())
            result.unlink()
            (process / 'cgroup').write_text('0::/system.slice/owned.service\n')
            self.assertIsNone(self.worker.read_worker_result(target, unit, root / 'proc')['observations'],
                              'Initial manager startup is not completed acceptance')

    def test_runtime_storage_requires_the_owned_bounded_mount(self):
        from types import SimpleNamespace
        validator = getattr(self.worker, 'validate_runtime_mount', None)
        self.assertTrue(callable(validator), 'Independent runtime mount validation is missing')
        source = SimpleNamespace(st_dev=12, st_ino=34, st_uid=2202, st_gid=2202, st_mode=0o40700)
        filesystem = SimpleNamespace(f_blocks=65536, f_frsize=4096)
        validator(self.target(), source, source, 12, filesystem)
        for replacement in ({'st_dev': 13}, {'st_ino': 35}, {'st_uid': 0},
                             {'st_gid': 0}, {'st_mode': 0o40755}):
            runtime = SimpleNamespace(**{**vars(source), **replacement})
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                validator(self.target(), source, runtime, 12, filesystem)
        with self.assertRaises(ValueError):
            validator(self.target(), source, source, 13, filesystem)
        with self.assertRaises(ValueError):
            validator(self.target(), source, source, 12, SimpleNamespace(f_blocks=65537, f_frsize=4096))

    def test_offline_policy_allows_local_tarballs_and_rejects_registry_images(self):
        policy = json.loads(self.worker.worker_files(self.target(), 'trusted launcher')['etc/containers/policy.json'])
        self.assertEqual([{'type': 'reject'}], policy['default'])
        self.assertEqual({'tarball': {'': [{'type': 'insecureAcceptAnything'}]}}, policy['transports'])

    def test_private_runtime_storage_uses_standard_uid_path(self):
        import tomllib
        files = self.worker.worker_files(self.target(), 'trusted launcher')
        storage = tomllib.loads(files['etc/containers/storage.conf'])['storage']
        self.assertEqual('/run/user/2202/storage', storage['runroot'])
        self.assertEqual('/work/graph', storage['graphroot'])

    def test_runtime_maps_require_the_declared_subordinate_ranges(self):
        from scripts.forgejo_runner import worker_probe
        mappings = {'uidmap': [{'container_id': 0, 'host_id': 2202, 'size': 1},
                               {'container_id': 1, 'host_id': 1065536, 'size': 65536}],
                    'gidmap': [{'container_id': 0, 'host_id': 2202, 'size': 1},
                               {'container_id': 1, 'host_id': 1065536, 'size': 65536}]}
        worker_probe.validate_mappings(mappings, self.target()['worker'])
        for kind in ('uidmap', 'gidmap'):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                worker_probe.validate_mappings({**mappings, kind: mappings[kind][:1]}, self.target()['worker'])

    def test_launcher_handoff_uses_worker_identity_and_drops_setup_privilege(self):
        import os
        from scripts.forgejo_runner import worker_launch
        class Handoff(Exception):
            pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'worker.json').write_text(json.dumps({'worker': self.target()['worker']}))
            captured = {}
            def mount(argv, **kwargs):
                if argv[:3] == ['/usr/bin/mount', '--bind', '/work/run']:
                    captured['runtime_bind'] = argv[2:]
            def handoff(path, argv):
                captured.update(argv=argv, user=os.environ.get('USER'), logname=os.environ.get('LOGNAME'),
                    runtime=os.environ.get('XDG_RUNTIME_DIR'), bus=os.environ.get('DBUS_SESSION_BUS_ADDRESS'))
                raise Handoff
            with patch.object(worker_launch, 'Path', side_effect=lambda p: root / p.lstrip('/')), \
                 patch.object(worker_launch.os, 'geteuid', return_value=0), \
                 patch.object(worker_launch.os, 'getpid', return_value=1), \
                 patch.object(worker_launch.os, 'chown'), \
                 patch.object(worker_launch.subprocess, 'run', side_effect=mount), \
                 patch.object(worker_launch.os, 'execv', side_effect=handoff), \
                 patch.dict(os.environ, {'USER': 'root', 'LOGNAME': 'root'}), self.assertRaises(Handoff):
                worker_launch.main()
            self.assertEqual('fixture-worker', captured['user'])
            self.assertEqual('fixture-worker', captured['logname'])
            self.assertEqual(['/work/run', '/run/user/2202'], captured.get('runtime_bind'))
            self.assertEqual('/run/user/2202', captured['runtime'])
            self.assertEqual('unix:path=/run/user/2202/bus', captured['bus'])
            self.assertEqual(['/usr/bin/setpriv', '--reuid=2202', '--regid=2202', '--clear-groups',
                '--bounding-set=-all,+setuid,+setgid', '--inh-caps=-all', '--ambient-caps=-all',
                '/usr/bin/catatonit', '--', '/usr/lib/systemd/systemd', '--user',
                '--log-target=console', '--log-level=warning'], captured['argv'])

    def test_launcher_rejects_ignored_pid_isolation_before_worker_setup(self):
        from scripts.forgejo_runner import worker_launch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'worker.json').write_text(json.dumps({'worker': self.target()['worker']}))
            with patch.object(worker_launch, 'Path', side_effect=lambda p: root / p.lstrip('/')), \
                 patch.object(worker_launch.os, 'geteuid', return_value=0), \
                 patch.object(worker_launch.os, 'getpid', return_value=2), \
                 patch.object(worker_launch.subprocess, 'run', side_effect=AssertionError('Setup began before PID isolation')), \
                 self.assertRaises(ValueError):
                worker_launch.main()

    def test_cleanup_observes_subordinate_processes_without_signalling_them(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            process = proc / '4321'; process.mkdir()
            (process / 'status').write_text('Uid:\t1065536 1065536 1065536 1065536\n')
            self.assertTrue(self.worker.worker_cleanup_errors(self.target(), proc))
            self.assertTrue(process.is_dir(), 'An ambiguous process must be preserved')
            (process / 'status').write_text('Uid:\t42 42 42 42\n')
            self.assertEqual([], self.worker.worker_cleanup_errors(self.target(), proc))

    def test_partial_worker_probe_does_not_open_full_host_acceptance(self):
        from scripts.forgejo_runner import host_fixture
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=root / '.tmp') as directory:
            descriptor = Path(directory) / 'target.json'
            descriptor.write_text(json.dumps(self.target()))
            outcome = {'exit_code': 0, 'primary_error': None, 'cleanup_errors': [],
                       'acceptance': 'worker-only'}
            with patch.object(host_fixture, 'inspect_workers', return_value=outcome):
                self.assertEqual(0, host_fixture.run(None, descriptor, worker_probe_only=True))
                with self.assertRaises(RuntimeError):
                    host_fixture.run(None, descriptor)


if __name__ == '__main__':
    unittest.main()
