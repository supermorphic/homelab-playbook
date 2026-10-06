"""Combined acceptance cannot substitute separate job and network receipts."""

import unittest
from pathlib import Path
from unittest.mock import patch

from test_host_network import network_target


class NativeAcceptanceTests(unittest.TestCase):
    def test_combined_inspection_requires_all_twenty_five_observations(self):
        from scripts.forgejo_runner import host_fixture
        target = network_target()
        target['limits'].update(memory_bytes=2 * 1024**3, disk_bytes=8 * 1024**3,
                                pids=512, cpu_percent=50, job_seconds=600)
        fields = ('rootless_api', 'sibling_containers', 'mapped_bind', 'private_loopback',
                  'published_loopback', 'user_manager', 'bounded_storage', 'outer_limits',
                  'process_boundary', 'allowed_ipv4', 'allowed_ipv6', 'denied_ipv4',
                  'denied_ipv6', 'policy_immutable', 'alternate_network_modes',
                  'kernel_policy', 'controlled_peer', 'dns', 'forgejo_https',
                  'public_http', 'public_https', 'gateway_boundary', 'one_job', 'job_runtime', 'job_public_access')
        outcome = {'acceptance': 'native-only', 'exit_code': 0, 'primary_error': None,
                   'cleanup_errors': [], 'observations': {field: True for field in fields}}
        configuration = {'source_tree': 'a' * 40, 'architecture': 'amd64', 'images': [], 'candidate': {}}
        def transport(target, payload, **kwargs):
            compile(payload, '<combined-native-payload>', 'exec')
            return {'architecture': 'amd64'} if not payload.splitlines()[-1].startswith('print(json.dumps(run_image_probe(') else outcome
        # The external SSH transport and asset exporter are the dependency
        # boundary; descriptor, payload construction and result gate are real.
        with patch('scripts.forgejo_runner.native_assets.prepare', return_value=({}, configuration)), \
             patch.object(host_fixture, 'ssh_observation', side_effect=transport):
            result = host_fixture.inspect_native_jobs(target, Path('.tmp/images.json'), public_network=True)
            self.assertEqual(outcome['observations'], result['observations'])
            for field in fields:
                bad = {**outcome, 'observations': {**outcome['observations'], field: False}}
                with self.subTest(field=field), patch.object(host_fixture, 'ssh_observation',
                        side_effect=lambda t,p,**kw: {'architecture':'amd64'} if not p.splitlines()[-1].startswith('print(json.dumps(run_image_probe(') else bad), \
                        self.assertRaises(ValueError):
                    host_fixture.inspect_native_jobs(target, Path('.tmp/images.json'), public_network=True)
