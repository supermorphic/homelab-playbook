"""Controlled reachable endpoints and kernel observations prove denial."""

import importlib
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_host_fixture import target_descriptor


def network_target():
    target = target_descriptor()
    target['limits'].update(memory_bytes=512 * 1024**2, disk_bytes=512 * 1024**2,
                            pids=256, cpu_percent=50, job_seconds=120)
    target['allowed_endpoints'] = [
        {'address': '192.0.2.20', 'port': 8100},
        {'address': '2001:db8:57::20', 'port': 8100}]
    target['denied_endpoints'] = [
        {'address': '10.57.0.2', 'port': 8100},
        {'address': 'fd57::2', 'port': 8100},
        {'address': '192.0.2.20', 'port': 8101},
        {'address': '2001:db8:57::20', 'port': 8101}]
    return target


class HostNetworkTests(unittest.TestCase):
    def setUp(self):
        try:
            self.network = importlib.import_module('scripts.forgejo_runner.host_network')
        except ModuleNotFoundError:
            self.fail('Controlled network acceptance is missing')

    def test_arbitrary_endpoint_cannot_be_used_as_synthetic_peer(self):
        target = network_target()
        self.network.validate_network_target(target)
        for key in ('allowed_endpoints', 'denied_endpoints'):
            for replacement in ({'address': '127.0.0.1', 'port': 8100},
                                {'address': '198.51.100.10', 'port': 443}):
                with self.subTest(key=key, replacement=replacement), self.assertRaises(ValueError):
                    self.network.validate_network_target({**target, key: [replacement]})

    def test_missing_dual_stack_or_port_case_fails_before_setup(self):
        target = network_target()
        for key in ('allowed_endpoints', 'denied_endpoints'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.network.validate_network_target({**target, key: target[key][:-1]})

    def test_reachable_denied_listener_is_a_failure(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0)); listener.listen()
        endpoint = {'address': '127.0.0.1', 'port': listener.getsockname()[1]}
        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.sendall(b'owned-network-endpoint\n')
        thread = threading.Thread(target=serve, daemon=True); thread.start()
        try:
            with self.assertRaises(ValueError):
                self.network.check_connections([], [endpoint])
        finally:
            listener.close(); thread.join(timeout=2)

    def test_allowed_listener_requires_the_owned_response(self):
        for payload in (b'owned-network-endpoint\n', b'unrelated-response\n'):
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0)); listener.listen()
            endpoint = {'address': '127.0.0.1', 'port': listener.getsockname()[1]}
            def serve():
                connection, _ = listener.accept()
                with connection:
                    connection.sendall(payload)
            thread = threading.Thread(target=serve, daemon=True); thread.start()
            try:
                if payload.startswith(b'owned-'):
                    self.network.check_connections([endpoint], [])
                else:
                    with self.assertRaises(ValueError):
                        self.network.check_connections([endpoint], [])
            finally:
                listener.close(); thread.join(timeout=2)

    def test_connection_refused_is_not_evidence_of_policy_denial(self):
        listener = socket.socket(); listener.bind(('127.0.0.1', 0))
        endpoint = {'address': '127.0.0.1', 'port': listener.getsockname()[1]}
        listener.close()
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection((endpoint['address'], endpoint['port']), timeout=1)
        with self.assertRaises(ConnectionRefusedError):
            self.network.check_connections([], [endpoint])

    def test_zero_kernel_counters_cannot_count_as_acceptance(self):
        policy = {'nftables': [
            {'chain': {'family': 'inet', 'table': 'forgejo_fixture', 'name': 'output',
                       'hook': 'output', 'policy': 'drop'}},
            {'counter': {'family': 'inet', 'table': 'forgejo_fixture', 'name': 'denied4', 'packets': 6}},
            {'counter': {'family': 'inet', 'table': 'forgejo_fixture', 'name': 'denied6', 'packets': 6}}]}
        self.network.validate_policy_observation(policy)
        for counter in (1, 2):
            policy['nftables'][counter]['counter']['packets'] = 0
            with self.assertRaises(ValueError):
                self.network.validate_policy_observation(policy)
            policy['nftables'][counter]['counter']['packets'] = 6
        policy['nftables'][0]['chain']['policy'] = 'accept'
        with self.assertRaises(ValueError):
            self.network.validate_policy_observation(policy)

    def test_peer_must_be_unprivileged_and_in_a_separate_private_namespace(self):
        self.assertTrue(callable(getattr(self.network, 'validate_peer_records', None)))
        target = network_target()
        outer = '/system.slice/forgejo-worker-synthetic.service'
        peer = {'uid': target['controller']['uid'], 'gid': target['controller']['gid'],
                'net': 789, 'pidns': 456, 'cgroup': outer,
                'effective_caps': 0, 'permitted_caps': 0, 'bounding_caps': 0,
                'inheritable_caps': 0, 'ambient_caps': 0}
        self.network.validate_peer_records(target, [peer], outer, 123, 456, 456)
        for changed in ({'effective_caps': 4096}, {'bounding_caps': 4096},
                        {'inheritable_caps': 4096}, {'ambient_caps': 4096},
                        {'net': 123}, {'net': 456}, {'pidns': 123},
                        {'cgroup': outer + '-unrelated'}, {'uid': 0}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.network.validate_peer_records(target, [{**peer, **changed}], outer, 123, 456, 456)
        with self.assertRaises(ValueError):
            self.network.validate_peer_records(target, [], outer, 123, 456, 456)

    def test_listener_observation_handles_both_address_families(self):
        self.assertTrue(callable(getattr(self.network, 'listening_endpoints', None)))
        output = ('LISTEN 0 8 192.0.2.20:8100 0.0.0.0:*\n'
                  'LISTEN 0 8 [2001:db8:57::20]:8100 [::]:*\n')
        self.assertEqual({('192.0.2.20', 8100), ('2001:db8:57::20', 8100)},
                         self.network.listening_endpoints(output))

    def test_network_setup_rejects_host_namespace_before_commands(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        try:
            setup = importlib.import_module('scripts.forgejo_runner.network_setup')
        except ModuleNotFoundError:
            self.fail('Trusted namespace-local setup is missing')
        with patch.object(setup.os, 'stat', return_value=SimpleNamespace(st_ino=123)), \
             patch.object(setup.subprocess, 'run', side_effect=AssertionError('Mutation began')), \
             self.assertRaises(ValueError):
            setup.configure({'host_network_inode': 123})

    def test_success_requires_all_network_and_worker_evidence(self):
        from scripts.forgejo_runner import host_fixture
        fields = ('rootless_api', 'sibling_containers', 'mapped_bind', 'private_loopback',
                  'published_loopback', 'user_manager', 'bounded_storage', 'outer_limits',
                  'process_boundary', 'allowed_ipv4', 'allowed_ipv6', 'denied_ipv4',
                  'denied_ipv6', 'policy_immutable', 'alternate_network_modes',
                  'kernel_policy', 'controlled_peer')
        outcome = {'acceptance': 'network-only', 'exit_code': 0, 'primary_error': None,
                   'cleanup_errors': [], 'observations': {field: True for field in fields}}
        inspector = getattr(host_fixture, 'inspect_network', None)
        self.assertTrue(callable(inspector), 'Registered network inspection is missing')
        def transport(target, payload, **kwargs):
            compile(payload, '<controlled-network-payload>', 'exec')
            return outcome
        with patch.object(host_fixture, 'ssh_observation', side_effect=transport):
            self.assertEqual(outcome, inspector(network_target()))
        for field in fields:
            with self.subTest(field=field):
                bad = {**outcome, 'observations': {**outcome['observations'], field: False}}
                with patch.object(host_fixture, 'ssh_observation', return_value=bad), self.assertRaises(ValueError):
                    inspector(network_target())

    def test_network_reserve_keeps_four_gib_for_existing_services(self):
        validator = getattr(self.network, 'validate_network_budget', None)
        self.assertTrue(callable(validator), 'Network service reserve guard is missing')
        target = network_target()
        capacity = {'memory_total': 8 * 1024**3, 'memory_available': 5 * 1024**3,
                    'disk_available': 10 * 1024**3}
        validator(target, capacity)
        for key in ('memory_available', 'memory_total', 'disk_available'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validator(target, {**capacity, key: 4 * 1024**3})

    def test_dirty_source_is_rejected_before_host_access(self):
        from scripts.forgejo_runner import host_fixture
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=root / '.tmp') as directory:
            descriptor = Path(directory) / 'target.json'
            descriptor.write_text(json.dumps(network_target()))
            with patch.object(host_fixture.subprocess, 'check_output', return_value=' M changed-source\n'), \
                 patch.object(host_fixture, 'inspect_network', side_effect=AssertionError('Host accessed')), \
                 self.assertRaises(ValueError):
                host_fixture.run(None, descriptor, network_probe_only=True)


if __name__ == '__main__':
    unittest.main()
