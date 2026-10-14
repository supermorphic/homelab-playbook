"""Host migration cannot publish a listener or change existing storage mappings."""
from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_archive as fixtures
from test_profiles import SOURCE, TARGET
from test_admission import Client as ProxyClient, HOST as HOSTNAME, ORIGINAL
from copy import deepcopy


class UpgradeHostTests(unittest.TestCase):
    def setUp(self):
        self.module = fixtures.load('upgrade_host')
        self.state = Path('/synthetic/forgejo')
        self.identifier = 'a' * 32

    def test_temporary_database_has_no_network_or_restart_and_preserves_uid_mapping(self):
        arguments = self.module.database_arguments(SOURCE, self.state, self.identifier)
        self.assertEqual('none', arguments[arguments.index('--network') + 1])
        self.assertEqual('no', arguments[arguments.index('--restart') + 1])
        self.assertEqual('keep-id:uid=999,gid=999', arguments[arguments.index('--userns') + 1])
        self.assertEqual(SOURCE['postgres_image'], arguments[-1])
        self.assertNotIn('--publish', arguments)

    def test_target_shares_only_isolated_loopback_and_preserves_application_mapping(self):
        arguments = self.module.application_arguments(TARGET, self.state,
            Path('/synthetic/maintenance-config'), self.identifier)
        network = arguments[arguments.index('--network') + 1]
        self.assertEqual('container:forgejo-upgrade-' + self.identifier + '-postgres', network)
        self.assertEqual('keep-id:uid=1000,gid=1000', arguments[arguments.index('--userns') + 1])
        self.assertEqual(TARGET['image'], arguments[-1])
        self.assertNotIn('--publish', arguments)
        self.assertNotIn('/var/run/docker.sock', ' '.join(arguments))

    def test_missing_or_unbound_queue_prerequisites_fail_closed(self):
        expected = {'id': self.identifier, 'source_generation': 'd' * 32}
        now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
        document = {'schema': 1, 'transaction_id': self.identifier,
                    'source_generation': 'd' * 32, 'observed_at': '2026-10-13T11:59:59Z',
                    'runners_drained': True, 'queues_flushed': True, 'mirrors_quiesced': True}
        gated = '2026-10-13T11:59:58.123456+00:00'
        self.module.validate_prerequisites(document, expected, now, gate_since=gated)
        with self.assertRaises(ValueError):
            self.module.validate_prerequisites(document, expected, now,
                                               gate_since='2026-10-13T11:59:59.123456+00:00')
        for field, value in (('transaction_id', 'b' * 32), ('source_generation', 'e' * 32),
                             ('observed_at', '2026-10-13T11:00:00Z'),
                             ('queues_flushed', False), ('runners_drained', False),
                             ('queues_flushed', 1), ('mirrors_quiesced', False)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.module.validate_prerequisites({**document, field: value}, expected, now, gate_since=gated)

    def test_death_after_gate_reapplication_invalidates_old_queue_attestation(self):
        with tempfile.TemporaryDirectory(dir=fixtures.ROOT / '.tmp') as temporary:
            root = Path(temporary)
            host = self.module.Host.__new__(self.module.Host)
            host.source = {'hostname': HOSTNAME, 'backend_port': 18081}
            host.proxy = ProxyClient()
            host.proxy.saved = True
            host.recovery = lambda saved: root
            host.write_file = lambda path, content: path.write_bytes(content)
            saved = {'id': self.identifier, 'source_generation': 'd' * 32}
            with patch.object(self.module.service, 'safe_read', side_effect=lambda path, **kwargs: path.read_bytes()):
                host.block_admission(saved)
                path = root / 'admission.json'
                record = json.loads(path.read_text())
                record['gated_at'] = '2026-10-13T11:59:58+00:00'
                path.write_text(json.dumps(record))
                host.proxy.routes[1] = deepcopy(ORIGINAL)
                replace = host.proxy.replace
                def interrupted(*args):
                    replace(*args)
                    raise RuntimeError('controller died after PATCH')
                host.proxy.replace = interrupted
                with self.assertRaises(RuntimeError):
                    host.block_admission(saved)
                self.assertNotIn('gated_at', json.loads(path.read_text()))
                host.proxy.replace = replace
                with patch.object(self.module, 'datetime', wraps=datetime) as clock:
                    clock.now.return_value = datetime(2026, 10, 13, 12, 0, 0, 1, tzinfo=timezone.utc)
                    host.block_admission(saved)
                renewed = json.loads(path.read_text())['gated_at']
                document = {'schema': 1, 'transaction_id': self.identifier,
                            'source_generation': 'd' * 32, 'observed_at': '2026-10-13T11:59:59Z',
                            'runners_drained': True, 'queues_flushed': True, 'mirrors_quiesced': True}
                with self.assertRaises(ValueError):
                    self.module.validate_prerequisites(document, saved,
                        datetime(2026, 10, 13, 12, tzinfo=timezone.utc), gate_since=renewed)

    def test_maintenance_configuration_blocks_cron_and_retargets_only_database_transport(self):
        text = '[database]\nHOST = forgejo-postgres:5432\nPASSWD = synthetic-fixture\n[cron]\nENABLED = true\n[server]\nROOT_URL = https://forgejo.infra.example.com/\n'
        import configparser
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(self.module.maintenance_config(text))
        self.assertEqual('127.0.0.1:5432', parser['database']['HOST'])
        self.assertEqual('synthetic-fixture', parser['database']['PASSWD'])
        self.assertFalse(parser.getboolean('cron', 'ENABLED'))
        self.assertEqual('https://forgejo.infra.example.com/', parser['server']['ROOT_URL'])

    def test_repeated_quiescence_preserves_original_timer_state(self):
        host = self.module.Host.__new__(self.module.Host)
        host.prerequisites = {}
        host.proxy = None
        saved = {'id': self.identifier, 'backup_timer_active': True}
        host.recovery = lambda saved: Path('/synthetic/recovery')
        host.assert_no_running_jobs = lambda: None
        with patch.object(self.module.admission, 'require_gated'), \
                patch.object(self.module, 'validate_prerequisites'), \
                patch.object(self.module.service, 'safe_read', return_value=b'{}'), \
                patch.object(self.module.upgrade, 'write'), \
                patch.object(self.module.service, 'manager', return_value=SimpleNamespace(returncode=3)):
            host.quiesce(saved)
        self.assertTrue(saved['backup_timer_active'])

    def test_timer_restore_intent_is_durable_before_stopping_timer(self):
        host = self.module.Host.__new__(self.module.Host)
        host.prerequisites = {}
        host.proxy = None
        host.recovery = lambda saved: Path('/synthetic/recovery')
        host.assert_no_running_jobs = lambda: None
        saved = {'id': self.identifier}
        durable = {}
        def manager(arguments, **kwargs):
            if arguments == ['stop', 'forgejo-backup.timer'] and durable.get('backup_timer_active') is not True:
                raise AssertionError('timer was changed before its restore intent was durable')
            return SimpleNamespace(returncode=0 if arguments == ['is-active', 'forgejo-backup.timer'] else 3)
        def persist(host, document):
            durable.update(document)
        with patch.object(self.module.admission, 'require_gated'), \
                patch.object(self.module, 'validate_prerequisites'), \
                patch.object(self.module.service, 'safe_read', return_value=b'{}'), \
                patch.object(self.module.upgrade, 'write', side_effect=persist), \
                patch.object(self.module.service, 'manager', side_effect=manager):
            host.quiesce(saved)
        self.assertTrue(durable['backup_timer_active'])

    def test_interrupted_source_preservation_rebuilds_until_every_input_is_sealed(self):
        with tempfile.TemporaryDirectory(dir=fixtures.ROOT / '.tmp') as temporary:
            root = Path(temporary)
            helpers, state = root / 'helpers', root / 'state'
            helpers.mkdir()
            (state / 'config').mkdir(parents=True)
            source = {**SOURCE, 'recovery_generation': 'd' * 32}
            (helpers / 'desired.json').write_text(json.dumps(source))
            (helpers / 'helper-generation.json').write_text(json.dumps({'generation': 'bundle'}))
            bundle = helpers / 'bundles/bundle'
            bundle.mkdir(parents=True)
            (bundle / 'service.py').write_text('independent recovery helper')
            control = root / 'unit'
            control.write_text('independent source definition')
            keys = ('database-password', 'secret-key', 'internal-token', 'lfs-jwt-secret', 'oauth2-jwt-secret')
            for name in keys:
                (state / 'config' / name).write_text('synthetic-' + name)
            host = self.module.Host.__new__(self.module.Host)
            host.helpers, host.allocation = helpers, {}
            host.private_dir = lambda path: path.mkdir(parents=True, exist_ok=True)
            saved = {'id': self.identifier, 'source': SOURCE, 'source_generation': 'd' * 32}
            destination = host.recovery(saved)
            def write(path, content, **kwargs):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            writes = 0
            def interrupted(path, content, **kwargs):
                nonlocal writes
                write(path, content)
                writes += 1
                if path.name == 'source.json':
                    raise RuntimeError('controller died after source metadata publication')
            with patch.object(self.module.service, 'STATE', state), \
                    patch.object(self.module.service, 'safe_read', side_effect=lambda path, **kwargs: path.read_bytes()), \
                    patch.object(self.module.service, 'targets_for', return_value={'unit': control}), \
                    patch.object(self.module.install, 'FILES', ('service.py',)):
                host.write_file = interrupted
                with self.assertRaises(RuntimeError):
                    host.preserve(saved)
                host.write_file = write
                host.preserve(saved)
                for name in keys:
                    self.assertEqual('synthetic-' + name, (destination / 'settings' / name).read_text())
                self.assertTrue((destination / 'COMPLETE').is_file())
                (destination / 'settings/secret-key').unlink()
                with self.assertRaises(ValueError):
                    host.preserve(saved)
