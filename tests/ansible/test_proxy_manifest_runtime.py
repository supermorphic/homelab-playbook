"""Desired declaration authority is separate from deferred effective routes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

from test_reverse_proxy_activation import ActivationFixture

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('proxy_config', ROOT / 'roles/reverse_proxy/filter_plugins/proxy.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
sys.modules['proxy_config'] = renderer


def manifest_module():
    spec = importlib.util.spec_from_file_location('proxy_manifest', ROOT / 'roles/reverse_proxy/files/manifest.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManifestTests(ActivationFixture):
    def setUp(self):
        super().setUp()
        self.manifests = manifest_module().Manifest(self.activation)
        self.config = {'bind_addresses': ['10.20.30.40'], 'client_sources': ['10.20.0.0/16'],
                       'deferred_certificates': ['infra'], 'routes': [
                           {'hostname': 'app.infra.example.com', 'backend_port': 18080,
                            'certificate_name': 'infra'}]}

    def candidates(self):
        raw = json.dumps(self.config) + '\n'
        ingress = {'version': 1, 'manifest_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                   'listen_addresses': ['10.20.30.40'], 'https_client_networks': ['10.20.0.0/16']}
        self.write(self.state / 'desired.candidate.json', raw, 0o600)
        self.write(self.state / 'ingress.candidate.json', json.dumps(ingress), 0o600)
        return raw

    def test_preserves_hashed_original_manifest_and_defers_only_infra(self):
        raw = self.candidates()
        envelope, config = self.manifests.candidate()
        self.assertEqual(raw, envelope['manifest'])
        self.assertEqual([], self.manifests.effective(config)['routes'])
        self.assertEqual(1, len(config['routes']))
        self.assertNotIn('https://', self.manifests.render(config))

    def test_health_route_uses_deferred_certificate_and_endpoint_binding(self):
        self.config['routes'].append({'hostname': 'caddy.infra.example.com',
                                      'certificate_name': 'infra', 'health': True})
        self.candidates()
        unused, config = self.manifests.candidate()
        self.assertEqual([], self.manifests.effective(config)['routes'])
        self.assertIn(('caddy.infra.example.com', '10.20.30.40', 443),
                      self.manifests.endpoints(config))
        generation = '/etc/caddy/tls/infra/issued-1'
        effective = self.manifests.effective(config, generation)
        self.assertEqual(2, len(effective['routes']))
        rendered = self.manifests.render(config, generation)
        self.assertIn('https://caddy.infra.example.com:443', rendered)
        self.assertIn(generation + '/fullchain.pem', rendered)

    def test_hash_or_ingress_mismatch_cannot_become_authority(self):
        self.candidates()
        ingress_path = self.state / 'ingress.candidate.json'
        for field, value in [('manifest_sha256', 'f' * 64), ('listen_addresses', ['10.20.30.41']),
                             ('https_client_networks', ['10.0.0.0/8']), ('version', True)]:
            self.candidates()
            ingress = json.loads(ingress_path.read_text())
            ingress[field] = value
            self.write(ingress_path, json.dumps(ingress), 0o600)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.manifests.candidate()

    def test_missing_other_certificate_remains_in_effective_candidate(self):
        self.config['routes'].append({'hostname': 'other.example.com', 'backend_port': 18081,
                                      'certificate_name': 'other'})
        self.assertEqual(['other'], [route['certificate_name']
                                    for route in self.manifests.effective(self.config)['routes']])

    def test_broken_infra_pointer_is_not_treated_as_first_publication(self):
        base = self.root / 'etc/caddy/tls/infra'
        base.mkdir(mode=0o750)
        (base / 'current').symlink_to('missing')
        self.assertEqual(1, len(self.manifests.effective(self.config)['routes']))

    def test_candidate_replacement_is_detected_before_commit(self):
        self.candidates()
        envelope, unused = self.manifests.candidate()
        self.config['routes'][0]['backend_port'] = 18082
        self.candidates()
        with self.assertRaises(ValueError):
            self.manifests.unchanged(envelope)

    def test_policy_binding_covers_every_infra_hostname_and_address(self):
        self.config['bind_addresses'].append('fd00::40')
        expected = {('app.infra.example.com', '10.20.30.40', 443),
                    ('app.infra.example.com', 'fd00::40', 443)}
        self.assertEqual(expected, self.manifests.endpoints(self.config))

class DesiredActivationTests(ManifestTests):
    def setUp(self):
        super().setUp()
        sys.modules['proxy_manifest'] = manifest_module()
        run = self.commands.run
        def commands(argv):
            if argv[:6] == ['/usr/sbin/runuser', '-u', 'caddy', '--', '/usr/bin/caddy', 'adapt']:
                return json.dumps(self.old).encode()
            if argv[:6] == ['/usr/sbin/runuser', '-u', 'caddy', '--', '/usr/bin/caddy', 'reload']:
                self.commands.reloads += 1
                self.commands.loaded = self.old
                return b''
            return run(argv)
        self.commands.run = commands
        self.write(self.boot, self.manifests.render(self.config))

    def test_deferred_manifest_commits_even_when_boot_is_unchanged(self):
        raw = self.candidates()
        self.assertEqual('changed', self.activation.apply(desired=True))
        self.assertEqual(raw, json.loads((self.state / 'desired.json').read_bytes())['manifest'])
        self.assertEqual(0, self.commands.reloads)
        self.assertEqual('unchanged', self.activation.apply(desired=True))

    def test_interruption_restores_previous_desired_authority_with_boot(self):
        self.candidates()
        previous = self.boot.read_bytes()
        original = self.activation.durable_write
        def interrupted(path, *args):
            original(path, *args)
            if path.name == 'desired.json':
                raise KeyboardInterrupt()
        self.activation.durable_write = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.activation.apply(desired=True)
        self.activation.durable_write = original
        self.activation.recover()
        self.assertEqual(previous, self.boot.read_bytes())
        self.assertFalse((self.state / 'desired.json').exists())

    def test_pending_tls_blocks_desired_and_legacy_apply_before_reload(self):
        self.candidates()
        journal = self.root / 'etc/caddy/tls/.homelab-tls-private/transaction.json'
        journal.parent.mkdir(mode=0o700)
        journal.write_text('{}')
        for desired in (True, False):
            with self.assertRaises(self.module.ActivationError):
                self.activation.apply(desired=desired)
        self.assertEqual(0, self.commands.reloads)

    def test_marker_cleanup_failure_restores_desired_from_memory(self):
        self.candidates()
        def cleanup_failure():
            self.activation.pending.unlink()
            raise OSError('injected marker durability failure')
        self.activation.clear_pending = cleanup_failure
        with self.assertRaises(self.module.ActivationError):
            self.activation.apply(desired=True)
        self.assertFalse((self.state / 'desired.json').exists())
