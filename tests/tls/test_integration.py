"""Joint publication recovery uses the real Publisher journal and filesystem."""
import json
import io
from contextlib import nullcontext, redirect_stderr
from pathlib import Path
import unittest

import test_publication as publication_tests
from tls_runtime.publication import Publisher


class BoundPublicationTests(unittest.TestCase):
    setUp = publication_tests.PublicationTests.setUp
    fingerprint = staticmethod(publication_tests.PublicationTests.fingerprint)
    reload_and_verify = publication_tests.PublicationTests.reload_and_verify
    preflight = publication_tests.PublicationTests.preflight

    def integrated(self, **kwargs):
        return Publisher(self.root, self.journal, self.owner_uid, self.reader_gid,
                         self.fingerprint, self.reload_and_verify, self.preflight,
                         prepare=lambda record: {'desired_revision': 'a' * 64,
                             'previous_boot': 'old\n', 'candidate_boot': 'new\n',
                             'disk_recovery': None, 'restart': False,
                             'activation_failed': False, 'restoration_failed': False}, **kwargs)

    def test_binding_is_durable_before_pointer_switch(self):
        publisher = self.integrated()
        switch = publisher._switch_current
        def check(generation):
            record = json.loads(self.journal.read_bytes())
            self.assertEqual('old\n', record['caddy']['previous_boot'])
            self.assertEqual('prepared', record['status'])
            switch(generation)
        publisher._switch_current = check
        self.assertTrue(publisher.publish(b'certificate-one', b'private-key-one'))

    def test_disk_restoration_retries_prepared_first_candidate_without_issuance(self):
        publisher = self.integrated()
        original = publisher._switch_current
        publisher._switch_current = lambda name: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            publisher.publish(b'certificate-one', b'private-key-one')
        record = json.loads(self.journal.read_bytes())
        record['caddy']['disk_recovery'] = 'restored'
        publisher._write_journal(record)
        publisher._switch_current = original
        result = publisher.recover()
        self.assertEqual('changed', result['publication'])
        self.assertFalse(result['activation_failed'])
        self.assertFalse(result['restoration_failed'])
        self.assertEqual(b'certificate-one', (self.root / 'current/fullchain.pem').read_bytes())
        self.assertFalse(self.journal.exists())

import hashlib
import importlib.util
import os
import re
import sys
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ansible'))
from test_reverse_proxy_activation import ActivationFixture
import test_reverse_proxy_activation as proxy_tests


class AdapterDiagnosticTests(unittest.TestCase):
    def test_safe_activation_failure_reaches_operator(self):
        from tls_runtime import caddy
        module = proxy_tests.load_activation()
        activator = mock.Mock()
        activator.locked.return_value = nullcontext()
        output = io.StringIO()
        with mock.patch.object(caddy, 'activation_module', return_value=module), \
                mock.patch.object(module, 'Activator', return_value=activator), \
                mock.patch.object(caddy.os, 'geteuid', return_value=0), \
                mock.patch.object(caddy, 'Integration') as integration, redirect_stderr(output):
            integration.return_value.validate_candidate.side_effect = module.ActivationError(
                'external certificate is not currently valid')
            self.assertEqual(1, caddy.main(['validate', '/synthetic/candidate']))
        self.assertIn('TLS Caddy validate failed: external certificate is not currently valid', output.getvalue())

    def test_unclassified_failure_does_not_expose_exception_contents(self):
        from tls_runtime import caddy
        module = proxy_tests.load_activation()
        activator = mock.Mock()
        activator.locked.return_value = nullcontext()
        output = io.StringIO()
        with mock.patch.object(caddy, 'activation_module', return_value=module), \
                mock.patch.object(module, 'Activator', return_value=activator), \
                mock.patch.object(caddy.os, 'geteuid', return_value=0), \
                mock.patch.object(caddy, 'Integration') as integration, redirect_stderr(output):
            integration.return_value.validate_candidate.side_effect = ValueError('synthetic-private-value')
            self.assertEqual(1, caddy.main(['validate', '/synthetic/candidate']))
        self.assertNotIn('synthetic-private-value', output.getvalue())
        self.assertIn('TLS Caddy integration failed', output.getvalue())


class AdapterFixture(ActivationFixture):
    configure_certificate = proxy_tests.CertificateTests.configure_certificate

    def setUp(self):
        super().setUp()
        from test_proxy_manifest_runtime import manifest_module
        sys.modules['proxy_manifest'] = manifest_module()
        from tls_runtime.caddy import Integration
        self.integration = Integration(self.activation)
        (self.root / 'etc/caddy/tls/infra').mkdir(mode=0o750)
        (self.root / 'etc/caddy/tls/.homelab-tls-private').mkdir(mode=0o700)
        self.config = {'bind_addresses': ['10.20.30.40'], 'client_sources': ['10.20.0.0/16'],
                       'deferred_certificates': ['infra'], 'routes': [
                           {'hostname': 'app.infra.example.com', 'backend_port': 18080,
                            'certificate_name': 'infra'}]}
        raw = json.dumps(self.config)
        self.envelope = {'version': 1, 'manifest': raw, 'ingress': {'version': 1,
                         'manifest_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                         'listen_addresses': ['10.20.30.40'], 'https_client_networks': ['10.20.0.0/16']}}
        self.write(self.state / 'desired.json', json.dumps(self.envelope), 0o600)
        base, version = self.configure_certificate()
        self.sans = b'X509v3 Subject Alternative Name:\n    DNS:*.infra.example.com\n'
        self.commands.ss = None
        original = self.commands.run
        def host(argv):
            if argv[:6] == ['/usr/sbin/runuser', '-u', 'caddy', '--', '/usr/bin/caddy', 'adapt']:
                content = Path(argv[argv.index('--config') + 1]).read_text()
                if content.startswith('{"'):
                    return content.encode()
                result = dict(self.old)
                hosts = re.findall(r'https://([a-z0-9.-]+):443', content)
                if hosts:
                    files = re.findall(r'\ttls (\S+) (\S+)', content)
                    server = {'listen': ['10.20.30.40:443'], 'protocols': ['h1', 'h2'],
                              'automatic_https': {'disable': True}, 'routes': [],
                              'tls_connection_policies': []}
                    pairs = []
                    for index, (hostname, pair) in enumerate(zip(hosts, files)):
                        tag = 'cert' + str(index)
                        server['routes'].append({'match': [{'host': [hostname]}]})
                        server['tls_connection_policies'].append({'match': {'sni': [hostname]},
                            'certificate_selection': {'any_tag': [tag]}})
                        pairs.append({'certificate': pair[0], 'key': pair[1], 'tags': [tag]})
                    result['apps'] = {'http': {'servers': {'srv0': server}},
                                      'tls': {'certificates': {'load_files': pairs}}}
                return json.dumps(result).encode()
            if argv[:6] == ['/usr/sbin/runuser', '-u', 'caddy', '--', '/usr/bin/caddy', 'reload']:
                self.commands.reloads += 1
                if self.commands.fail_reload:
                    self.commands.fail_reload = False
                    raise self.module.ActivationError('injected reload failure')
                adapt = list(argv)
                adapt[5] = 'adapt'
                self.commands.loaded = json.loads(host(adapt))
                return b''
            return original(argv)
        self.commands.run = host
        self.publisher = Publisher(self.integration.root, self.activation.tls_journal,
                                   os.getuid(), os.getgid(), publication_tests.PublicationTests.fingerprint,
                                   lambda fp: self.integration.activate('reload' if fp else 'deactivate'),
                                   self.integration.validate_candidate, prepare=self.integration.prepare)


class AdapterTests(AdapterFixture):
    def test_first_publication_activates_manifest_with_one_reload(self):
        with self.activation.locked():
            self.publisher.publish(b'certificate-one', b'private-key-one')
        self.assertIn('app.infra.example.com', self.boot.read_text())
        self.assertEqual(1, self.commands.reloads)
        self.assertFalse(self.activation.tls_journal.exists())

    def test_failed_first_publication_restores_disk_and_proves_no_listener(self):
        self.commands.fail_reload = True
        previous = self.boot.read_bytes()
        from tls_runtime.publication import PublicationError
        with self.activation.locked(), self.assertRaises(PublicationError) as failure:
            self.publisher.publish(b'certificate-one', b'private-key-one')
        self.assertFalse(failure.exception.restoration_failed)
        self.assertEqual(previous, self.boot.read_bytes())
        self.assertEqual(self.old, self.commands.loaded)
        self.assertFalse(os.path.lexists(self.integration.root / 'current'))

    def test_preflight_failure_never_selects_or_installs_candidate(self):
        self.commands.invalid = True
        previous = self.boot.read_bytes()
        with self.activation.locked(), self.assertRaises(self.module.ActivationError):
            self.publisher.publish(b'certificate-one', b'private-key-one')
        self.assertFalse(os.path.lexists(self.integration.root / 'current'))
        self.assertEqual(previous, self.boot.read_bytes())
        self.assertEqual(0, self.commands.reloads)

    def test_restart_is_requested_with_durable_rollback_authority(self):
        def broken(argv):
            if argv[:6] == ['/usr/sbin/runuser', '-u', 'caddy', '--', '/usr/bin/caddy', 'reload']:
                raise self.module.ActivationError('reload rejected')
            return original(argv)
        original = self.commands.run
        self.commands.run = broken
        self.commands.loaded = {'drift': True}
        from tls_runtime.publication import PublicationError
        with self.activation.locked(), self.assertRaises(PublicationError) as failure:
            self.publisher.publish(b'certificate-one', b'private-key-one')
        self.assertTrue(failure.exception.restoration_failed)
        self.assertTrue(self.publisher._read_journal()['caddy']['restart'])

    def test_adapter_diagnostics_reach_journal_on_initial_and_retry_calls(self):
        from tls_runtime import caddy
        from types import SimpleNamespace
        deployment = caddy.Deployment(self.activation, SimpleNamespace(
            reader_gid=os.getgid(), endpoints=[{
                'hostname': 'app.infra.example.com', 'address': '10.20.30.40', 'port': 443}]))
        for outcomes in ([1], [caddy.RESTART_EXIT, 1]):
            with self.subTest(outcomes=outcomes), deployment.locked(), \
                    mock.patch.object(caddy.subprocess, 'run', side_effect=[
                        SimpleNamespace(returncode=code) for code in outcomes]) as run, \
                    mock.patch.object(deployment, 'restart'), \
                    self.assertRaisesRegex(RuntimeError, 'fixed Caddy integration operation failed'):
                try:
                    deployment.operation('reload')
                finally:
                    self.assertEqual(len(outcomes), run.call_count)
                    for call in run.call_args_list:
                        self.assertIsNone(call.kwargs['stderr'])
                        self.assertEqual(caddy.subprocess.DEVNULL, call.kwargs['stdout'])

    def test_inherited_fd9_is_exclusive_and_wrong_inode_is_rejected(self):
        from tls_runtime import caddy
        from types import SimpleNamespace
        import fcntl
        policy = SimpleNamespace(reader_gid=os.getgid(), endpoints=[{
            'hostname': 'app.infra.example.com', 'address': '10.20.30.40', 'port': 443}])
        deployment = caddy.Deployment(self.activation, policy)
        script = self.root / 'fake-adapter'
        script.write_text('#!' + sys.executable + '\nimport fcntl, os, sys\n'
                          'assert sys.argv[1:] == ["reload"]\n'
                          'expected = os.stat(' + repr(str(self.activation.lock_path)) + ')\n'
                          'actual = os.fstat(9)\n'
                          'assert (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)\n'
                          'probe = os.open(' + repr(str(self.activation.lock_path)) + ', os.O_RDONLY)\n'
                          'try:\n fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)\n'
                          'except BlockingIOError:\n sys.exit(0)\n'
                          'sys.exit(1)\n')
        script.chmod(0o755)
        with mock.patch.object(caddy, 'ADAPTER', str(script)), deployment.locked():
            deployment.operation('reload')
            alternate = os.open(self.boot, os.O_RDONLY)
            saved = os.dup(9)
            try:
                os.dup2(alternate, 9)
                with self.assertRaises(self.module.ActivationError):
                    deployment.operation('reload')
            finally:
                os.dup2(saved, 9)
                os.close(saved)
                os.close(alternate)

class CoordinatorScopeTests(unittest.TestCase):
    def test_network_wait_is_outside_publication_lock(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from tls_runtime import runtime
        held = []
        @contextmanager
        def scope():
            held.append(True)
            try:
                yield
            finally:
                held.pop()
        def recover():
            self.assertEqual([True], held)
        def issue():
            self.assertEqual([], held)
        def snapshot():
            self.assertEqual([], held)
            return b'certificate', b'key'
        def publish(cert, key):
            self.assertEqual([True], held)
            return True
        result = runtime.reconcile(SimpleNamespace(recover=recover, publish=publish),
                                   issue, snapshot, publication_context=scope)
        self.assertEqual('changed', result['publication'])

class RestartAndInterruptionTests(AdapterFixture):
    def test_restart_releases_lock_and_rechecks_revision_after_startup(self):
        from tls_runtime.caddy import Deployment
        from types import SimpleNamespace
        import fcntl
        policy = SimpleNamespace(reader_gid=os.getgid(), endpoints=[{
            'hostname': 'app.infra.example.com', 'address': '10.20.30.40', 'port': 443}])
        deployment = Deployment(self.activation, policy)
        original = self.publisher._switch_current
        self.publisher._switch_current = lambda name: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.publisher.publish(b'certificate-one', b'private-key-one')
        self.publisher._switch_current = original
        record = self.publisher._read_journal()
        record['status'] = 'rollback_pending'
        record['caddy']['restart'] = True
        self.publisher._write_journal(record)
        calls = []
        host = self.commands.run
        def restart(argv):
            if argv == ['/usr/bin/systemctl', 'restart', 'caddy.service']:
                with self.activation.lock_path.open('rb') as probe:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(self.module.ActivationError):
                    self.activation.apply()
                self.activation.recover()
                self.commands.loaded = self.old
                calls.append('startup completed')
                return b''
            return host(argv)
        self.commands.run = restart
        with deployment.locked():
            deployment.restart(self.envelope['ingress']['manifest_sha256'])
        self.assertEqual(['startup completed'], calls)
        self.assertEqual('restored', self.publisher._read_journal()['caddy']['disk_recovery'])
        self.assertTrue(self.publisher._read_journal()['caddy']['restart'])
        self.integration.activate('deactivate')
        self.assertFalse(self.publisher._read_journal()['caddy']['restart'])

    def test_startup_repeats_each_interrupted_disk_boundary(self):
        original_switch = self.publisher._switch_current
        self.publisher._switch_current = lambda name: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.publisher.publish(b'certificate-one', b'private-key-one')
        self.publisher._switch_current = original_switch
        initial = self.publisher._read_journal()
        for boundary in range(1, 5):
            with self.subTest(boundary=boundary):
                self.publisher._write_journal(initial)
                self.publisher._switch_current(initial['generation'])
                self.write(self.boot, initial['caddy']['candidate_boot'])
                writes = []
                journal = self.integration.publisher._write_journal
                switch = self.integration.publisher._switch_current
                boot = self.activation.durable_write
                def after(operation):
                    def call(*args):
                        operation(*args)
                        writes.append(True)
                        if len(writes) == boundary:
                            raise KeyboardInterrupt()
                    return call
                self.integration.publisher._write_journal = after(journal)
                self.integration.publisher._switch_current = after(switch)
                self.activation.durable_write = after(boot)
                with self.assertRaises(KeyboardInterrupt):
                    self.integration.recover_disk()
                self.integration.publisher._write_journal = journal
                self.integration.publisher._switch_current = switch
                self.activation.durable_write = boot
                self.integration.recover_disk()
                self.assertEqual(initial['caddy']['previous_boot'], self.boot.read_text())
                self.assertFalse(os.path.lexists(self.integration.root / 'current'))
                self.assertTrue((self.integration.root / initial['generation']).exists())

class PublicationActivationTests(AdapterFixture):
    def test_same_material_reopens_once_without_creating_a_generation(self):
        with self.activation.locked():
            self.publisher.publish(b'certificate-one', b'private-key-one')
            before = os.readlink(self.integration.root / 'current')
            self.assertFalse(self.publisher.publish(b'certificate-one', b'private-key-one'))
        self.assertEqual(before, os.readlink(self.integration.root / 'current'))
        self.assertEqual(2, self.commands.reloads)

    def test_failed_first_keeps_unrelated_route_and_its_shared_listener(self):
        from tls_runtime.publication import PublicationError
        self.config['routes'].append({'hostname': 'other.example.com', 'backend_port': 18081,
                                     'certificate_name': 'app'})
        raw = json.dumps(self.config)
        self.envelope['manifest'] = raw
        self.envelope['ingress']['manifest_sha256'] = hashlib.sha256(raw.encode()).hexdigest()
        self.write(self.state / 'desired.json', json.dumps(self.envelope), 0o600)
        self.sans += b'    DNS:other.example.com\n'
        self.write(self.boot, self.activation.manifest().render(self.config))
        previous = self.boot.read_bytes()
        self.commands.loaded = self.activation.adapted(self.boot)
        activate = self.publisher.reload_and_verify
        def fail_after_first_activation(fingerprint):
            activate(fingerprint)
            if fingerprint:
                raise RuntimeError('injected endpoint verification failure')
        self.publisher.reload_and_verify = fail_after_first_activation
        with self.activation.locked(), self.assertRaises(PublicationError) as failure:
            self.publisher.publish(b'certificate-one', b'private-key-one')
        self.assertFalse(failure.exception.restoration_failed)
        self.assertEqual(previous, self.boot.read_bytes())
        active = self.commands.loaded
        self.assertNotIn('app.infra.example.com', json.dumps(active))
        self.assertIn('other.example.com', json.dumps(active))
        self.assertEqual({('10.20.30.40', 443)}, self.activation.expected_listeners(active))

    def test_renewal_failure_restores_previous_pointer_and_boot(self):
        from tls_runtime.publication import PublicationError
        with self.activation.locked():
            self.publisher.publish(b'certificate-one', b'private-key-one')
            previous = os.readlink(self.integration.root / 'current')
            boot = self.boot.read_bytes()
            activate = self.publisher.reload_and_verify
            def reject_new(fp):
                activate(fp)
                if (self.integration.root / 'current/fullchain.pem').read_bytes() == b'certificate-two':
                    raise RuntimeError('injected served fingerprint mismatch')
            self.publisher.reload_and_verify = reject_new
            with self.assertRaises(PublicationError) as failure:
                self.publisher.publish(b'certificate-two', b'private-key-two')
            self.assertFalse(failure.exception.restoration_failed)
        self.assertEqual(previous, os.readlink(self.integration.root / 'current'))
        self.assertEqual(boot, self.boot.read_bytes())

class RestartBindingTests(AdapterFixture):
    def test_restart_rejects_changed_candidate_boot_authority(self):
        from tls_runtime.caddy import Deployment
        from types import SimpleNamespace
        deployment = Deployment(self.activation, SimpleNamespace(reader_gid=os.getgid(), endpoints=[{
            'hostname': 'app.infra.example.com', 'address': '10.20.30.40', 'port': 443}]))
        self.publisher.reload_and_verify = lambda fp: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.publisher.publish(b'certificate-one', b'private-key-one')
        self.publisher._switch_current(None)
        record = self.publisher._read_journal()
        record['status'] = 'rollback_pending'
        record['caddy']['restart'] = True
        self.publisher._write_journal(record)
        original = self.commands.run
        def restart(argv):
            if argv == ['/usr/bin/systemctl', 'restart', 'caddy.service']:
                self.activation.recover()
                changed = self.publisher._read_journal()
                changed['caddy']['candidate_boot'] += '\n'
                self.publisher._write_journal(changed)
                self.commands.loaded = self.old
                return b''
            return original(argv)
        self.commands.run = restart
        with deployment.locked(), self.assertRaises(ValueError):
            deployment.restart(self.envelope['ingress']['manifest_sha256'])

class JournalBoundaryTests(AdapterFixture):
    def test_startup_rejects_nonprivate_journal_parent_before_disk_mutation(self):
        self.publisher.reload_and_verify = lambda fp: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.publisher.publish(b'certificate-one', b'private-key-one')
        previous = os.readlink(self.integration.root / 'current')
        boot = self.boot.read_bytes()
        self.activation.tls_journal.parent.chmod(0o750)
        with self.assertRaises((ValueError, self.module.ActivationError)):
            self.integration.recover_disk()
        self.assertEqual(previous, os.readlink(self.integration.root / 'current'))
        self.assertEqual(boot, self.boot.read_bytes())

class InterruptedPublicationTests(AdapterFixture):
    def exercise(self, phase, previous=True):
        if previous:
            with self.activation.locked():
                self.publisher.publish(b'certificate-one', b'private-key-one')
        original_reload = self.publisher.reload_and_verify
        old_pointer = self.publisher._current_name()
        old_boot = self.boot.read_bytes()
        triggered = []
        if phase in ('rollback_pending', 'rollback_failed', 'restored', 'retirement'):
            def failure(fp):
                if self.publisher._current_name() != old_pointer or phase == 'rollback_failed':
                    raise RuntimeError('injected activation boundary failure')
                return original_reload(fp)
            self.publisher.reload_and_verify = failure
        write = self.publisher._write_journal
        boot_write = self.activation.durable_write
        rename = os.replace
        def journal(record):
            write(record)
            if record['status'] == phase:
                triggered.append(True)
                raise KeyboardInterrupt()
        def boot(path, *args):
            boot_write(path, *args)
            if path == self.boot and phase == 'boot':
                triggered.append(True)
                raise KeyboardInterrupt()
        def retirement(source, target):
            rename(source, target)
            if (phase == 'retirement' and Path(target).name.startswith('retired-')):
                triggered.append(True)
                raise KeyboardInterrupt()
        self.publisher._write_journal = journal
        self.activation.durable_write = boot
        with mock.patch('os.replace', side_effect=retirement), self.activation.locked(), self.assertRaises(KeyboardInterrupt):
            self.publisher.publish(b'certificate-two', b'private-key-two')
        self.assertEqual([True], triggered)
        self.publisher._write_journal = write
        self.activation.durable_write = boot_write
        self.publisher.reload_and_verify = original_reload
        with self.activation.locked():
            self.integration.recover_disk()
            self.assertEqual(old_pointer, self.publisher._current_name())
            self.assertEqual(old_boot, self.boot.read_bytes())
            self.publisher.recover()
        self.assertFalse(self.activation.tls_journal.exists())
        if phase in ('restored', 'retirement'):
            self.assertEqual(old_pointer, self.publisher._current_name())
        else:
            self.assertEqual(b'certificate-two', (self.integration.root / 'current/fullchain.pem').read_bytes())

    def test_prepared_first_publication_recovers_retained_candidate(self):
        self.exercise('prepared', previous=False)

    def test_interrupted_switch_recovers_joint_configuration(self):
        self.exercise('switched')

    def test_interrupted_boot_write_recovers_joint_configuration(self):
        self.exercise('boot')

    def test_interrupted_rollback_pending_retains_candidate(self):
        self.exercise('rollback_pending')

    def test_interrupted_rollback_failure_retains_both_outcomes(self):
        self.exercise('rollback_failed')

    def test_interrupted_restored_record_completes_terminal_cleanup(self):
        self.exercise('restored')

    def test_interrupted_retirement_does_not_require_deleted_candidate(self):
        self.exercise('retirement')

class DirectPreparedRecoveryTests(AdapterFixture):
    def exercise(self, previous=False, phase="prepared"):
        from tls_runtime import runtime
        if previous:
            with self.activation.locked():
                self.publisher.publish(b'certificate-one', b'private-key-one')
        write = self.publisher._write_journal
        def interrupt_publication(record):
            write(record)
            if record['status'] == phase:
                raise KeyboardInterrupt()
        self.publisher._write_journal = interrupt_publication
        with self.activation.locked(), self.assertRaises(KeyboardInterrupt):
            self.publisher.publish(b'certificate-two', b'private-key-two')
        self.publisher._write_journal = write
        retained = self.publisher._read_journal()
        self.assertIsNone(retained['caddy']['disk_recovery'])
        def forbidden_issuance():
            self.fail('integrated recovery must not start issuance')
        def forbidden_snapshot():
            self.fail('integrated recovery must use its retained generation')
        reloads_before = self.commands.reloads
        result = runtime.reconcile(self.publisher, forbidden_issuance, forbidden_snapshot,
                                   publication_context=self.activation.locked)
        self.assertEqual(1, self.commands.reloads - reloads_before)
        self.assertEqual('not_run', result['issuance'])
        self.assertEqual('changed', result['publication'])
        self.assertFalse(result['activation_failed'])
        self.assertFalse(result['restoration_failed'])
        self.assertEqual(retained['generation'], os.readlink(self.integration.root / 'current'))
        self.assertEqual(b'certificate-two', (self.integration.root / 'current/fullchain.pem').read_bytes())
        self.assertIn('app.infra.example.com', json.dumps(self.commands.loaded))
        self.assertIn('app.infra.example.com', self.boot.read_text())
        self.assertFalse(self.activation.tls_journal.exists())

    def test_first_prepared_candidate_recovers_without_startup_or_issuance(self):
        self.exercise()

    def test_prepared_renewal_recovers_without_startup_or_issuance(self):
        self.exercise(previous=True)

    def test_first_switched_candidate_recovers_without_startup_or_issuance(self):
        self.exercise(phase='switched')

    def test_switched_renewal_recovers_without_startup_or_issuance(self):
        self.exercise(previous=True, phase='switched')


class DirectRestoredRecoveryTests(AdapterFixture):
    def test_terminal_restoration_reports_failure_without_startup_or_issuance(self):
        from tls_runtime import runtime
        with self.activation.locked():
            self.publisher.publish(b'certificate-one', b'private-key-one')
        previous = self.publisher._current_name()
        reload = self.publisher.reload_and_verify
        write = self.publisher._write_journal
        def activation_failure(fp):
            reload(fp)
            if self.publisher._current_name() != previous:
                raise RuntimeError('injected activation failure')
        def interrupt_terminal_cleanup(record):
            write(record)
            if record['status'] == 'restored':
                raise KeyboardInterrupt()
        self.publisher.reload_and_verify = activation_failure
        self.publisher._write_journal = interrupt_terminal_cleanup
        with self.activation.locked(), self.assertRaises(KeyboardInterrupt):
            self.publisher.publish(b'certificate-two', b'private-key-two')
        self.publisher.reload_and_verify = reload
        self.publisher._write_journal = write
        self.assertEqual('restored', self.publisher._read_journal()['status'])
        def forbidden():
            self.fail('terminal integrated recovery must not start issuance or snapshot')
        reloads_before = self.commands.reloads
        result = runtime.reconcile(self.publisher, forbidden, forbidden,
                                   publication_context=self.activation.locked)
        self.assertEqual('not_run', result['issuance'])
        self.assertEqual('failed', result['publication'])
        self.assertTrue(result['activation_failed'])
        self.assertFalse(result['restoration_failed'])
        self.assertEqual(previous, self.publisher._current_name())
        self.assertEqual(reloads_before, self.commands.reloads)
        self.assertFalse(self.activation.tls_journal.exists())
