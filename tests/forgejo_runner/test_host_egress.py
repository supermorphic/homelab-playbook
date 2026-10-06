"""Public transport cannot widen trusted exceptions or helper authority."""

import importlib
import unittest
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from test_host_network import network_target


class HostEgressTests(unittest.TestCase):
    def test_readiness_waits_for_a_precreated_owned_marker_and_has_a_deadline(self):
        wait = getattr(self.egress, 'wait_network_ready', None)
        self.assertTrue(callable(wait), 'The empty marker is not a readiness failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'work').mkdir()
            marker = root / 'work/network-ready'
            marker.write_text(''); marker.chmod(0o600)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            timer = threading.Timer(0.05, lambda: marker.write_text('controlled-network-ready\n'))
            try:
                timer.start()
                self.assertTrue(wait(descriptor, timeout=1))
                timer.join(timeout=1)
                marker.write_text('')
                with self.assertRaises(ValueError):
                    wait(descriptor, timeout=0.02)
                marker.write_text('unrelated readiness')
                with self.assertRaises(ValueError):
                    wait(descriptor, timeout=1)
                marker.unlink()
                outside = root / 'unrelated-marker'
                outside.write_text('controlled-network-ready\n')
                marker.symlink_to(outside)
                with self.assertRaises(OSError):
                    wait(descriptor, timeout=1)
            finally:
                timer.join(timeout=1)
                os.close(descriptor)

    def test_gateway_diagnostics_remain_in_the_pinned_image(self):
        creator = getattr(self.egress, 'gateway_error_stream', None)
        self.assertTrue(callable(creator), 'Size-bounded gateway diagnostics are missing')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'work').mkdir()
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with creator(descriptor) as stream:
                    stream.write(b'owned diagnostic')
                    self.assertEqual((root / 'work/gateway-error.log').stat().st_ino,
                                     os.fstat(stream.fileno()).st_ino)
                with self.assertRaises(FileExistsError):
                    creator(descriptor)
                (root / 'work/gateway-error.log').unlink()
                outside = root / 'unrelated.log'
                outside.write_bytes(b'unrelated')
                (root / 'work/gateway-error.log').symlink_to(outside)
                with self.assertRaises(OSError):
                    creator(descriptor)
                self.assertEqual(b'unrelated', outside.read_bytes())
            finally:
                os.close(descriptor)

    def test_success_requires_public_access_and_independent_gateway_evidence(self):
        from scripts.forgejo_runner import host_fixture
        inspector = getattr(host_fixture, 'inspect_egress', None)
        self.assertTrue(callable(inspector), 'Registered public-egress acceptance is missing')
        fields = ('rootless_api', 'sibling_containers', 'mapped_bind', 'private_loopback',
                  'published_loopback', 'user_manager', 'bounded_storage', 'outer_limits',
                  'process_boundary', 'allowed_ipv4', 'allowed_ipv6', 'denied_ipv4',
                  'denied_ipv6', 'policy_immutable', 'alternate_network_modes',
                  'kernel_policy', 'controlled_peer', 'dns', 'forgejo_https',
                  'public_http', 'public_https', 'gateway_boundary')
        outcome = {'acceptance': 'egress-only', 'exit_code': 0, 'primary_error': None,
                   'cleanup_errors': [], 'observations': {field: True for field in fields}}
        def transport(target, payload, **kwargs):
            compile(payload, '<public-egress-payload>', 'exec')
            return outcome
        with patch.object(host_fixture, 'ssh_observation', side_effect=transport):
            self.assertEqual(outcome, inspector(network_target()))
        for field in fields:
            with self.subTest(field=field):
                bad = {**outcome, 'observations': {**outcome['observations'], field: False}}
                with patch.object(host_fixture, 'ssh_observation', return_value=bad), self.assertRaises(ValueError):
                    inspector(network_target())

    def setUp(self):
        try:
            self.egress = importlib.import_module('scripts.forgejo_runner.host_egress')
        except ModuleNotFoundError:
            self.fail('Trusted public-egress acceptance is missing')

    def test_configuration_rejects_ambiguous_and_unsafe_exceptions(self):
        configuration = {'forgejo_addresses': ['10.57.1.20'],
                         'host_addresses': ['10.57.1.4', '2001:db8:57::4'],
                         'dns_address': '10.57.1.53'}
        self.assertEqual(configuration, self.egress.validate_configuration(configuration))
        for changed in ({'forgejo_addresses': []},
                        {'forgejo_addresses': ['127.0.0.1']},
                        {'forgejo_addresses': ['169.254.169.254']},
                        {'forgejo_addresses': ['::ffff:127.0.0.1']},
                        {'forgejo_addresses': ['10.57.1.20', '10.57.1.20']},
                        {'host_addresses': ['10.57.1.4; accept']},
                        {'dns_address': '0.0.0.0'},
                        {'dns_address': '::ffff:127.0.0.53'},
                        {'arbitrary_rule': 'accept'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.egress.validate_configuration({**configuration, **changed})

    def test_resolver_uses_one_explicit_host_nameserver_and_no_search_paths(self):
        self.assertEqual('127.0.0.53', self.egress.resolver_address(
            'search private.example\nnameserver 127.0.0.53\nnameserver 10.57.1.53\n'))
        for source in ('# no resolver\n', 'nameserver invalid\n', 'nameserver 0.0.0.0\n',
                       'nameserver 10.57.1.53 extra\n', 'nameserver ff02::1\n'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.egress.resolver_address(source)

    def test_gateway_requires_zero_authority_and_the_owned_runtime_boundary(self):
        expected = {'uid': 22002, 'gid': 22002, 'net': 100, 'pidns': 200,
                    'mnt': 300, 'root_device': 40, 'root_inode': 50,
                    'cgroup': '/system.slice/forgejo-worker-synthetic.service',
                    'effective_caps': 0, 'permitted_caps': 0, 'bounding_caps': 0,
                    'inheritable_caps': 0, 'ambient_caps': 0}
        arguments = ({'uid': 22002, 'gid': 22002}, expected['cgroup'],
                     {'net': 100, 'pidns': 200, 'mnt': 300, 'root_device': 40, 'root_inode': 50})
        self.egress.validate_gateway(expected, *arguments)
        for changed in ({'uid': 0}, {'gid': 0}, {'net': 101}, {'pidns': 201},
                        {'mnt': 301}, {'root_inode': 51}, {'root_device': 41},
                        {'cgroup': expected['cgroup'] + '-unrelated'},
                        {'effective_caps': 1}, {'permitted_caps': 1}, {'bounding_caps': 1},
                        {'inheritable_caps': 1}, {'ambient_caps': 1}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.egress.validate_gateway({**expected, **changed}, *arguments)

    def test_gateway_disables_automatic_forwarding_and_pins_namespace_authority(self):
        builder = getattr(self.egress, 'pasta_arguments', None)
        self.assertTrue(callable(builder), 'Trusted gateway command is missing')
        configuration = {'forgejo_addresses': ['10.57.1.20'],
                         'host_addresses': ['10.57.1.4'], 'dns_address': '127.0.0.53'}
        command = builder(configuration, {'uid': 22002, 'gid': 22002}, 19)
        for option in ('-t', '-u', '-T', '-U'):
            self.assertEqual('none', command[command.index(option) + 1])
        for option in ('--no-map-gw', '--no-splice', '--foreground'):
            self.assertIn(option, command)
        self.assertEqual('/work/gateway.netns', command[command.index('--netns') + 1])
        self.assertEqual('22002:22002', command[command.index('--runas') + 1])
        self.assertEqual('127.0.0.53', command[command.index('--dns-host') + 1])

    def test_namespace_mount_refuses_the_host_namespace_before_mutation(self):
        from types import SimpleNamespace
        try:
            launcher = importlib.import_module('scripts.forgejo_runner.gateway_launch')
        except ModuleNotFoundError:
            self.fail('Owned immutable namespace handoff is missing')
        with patch.object(launcher.os, 'fstat', return_value=SimpleNamespace(st_ino=123)), \
             patch.object(launcher.os, 'stat', return_value=SimpleNamespace(st_ino=123)), \
             patch.object(launcher.os, 'open', side_effect=AssertionError('Mutation began')), \
             self.assertRaises(ValueError):
            launcher.prepare_namespace(19)
