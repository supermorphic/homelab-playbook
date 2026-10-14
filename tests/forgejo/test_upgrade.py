"""Exercise durable migration ordering and failure boundaries with real state files."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import unittest

import test_archive as fixtures
from test_profiles import SOURCE, TARGET


class Host:
    def __init__(self, root):
        self.helpers = root / 'helpers'
        self.helpers.mkdir(mode=0o750)
        self.admin_uid = os.getuid()
        self.group = os.getgid()
        self.source = {**SOURCE, 'recovery_generation': 'd' * 32}
        self.target = dict(TARGET)
        self.controller_sha256 = 'e' * 64
        self.data = root / 'state'
        self.data.write_text('independent source state')
        self.archive = root / 'archive'
        self.running = True
        self.events = []
        self.fail = None

    def locks(self):
        return nullcontext()

    def identity(self):
        return {'pid': 42, 'boot': 'synthetic', 'ticks': '100'}

    def event(self, name):
        self.events.append(name)
        if self.fail == name:
            raise RuntimeError('synthetic ' + name + ' failure')

    def preflight(self, saved=None):
        self.event('preflight')

    def preserve(self, saved):
        self.event('preserve')

    def block_admission(self, saved):
        self.event('block-admission')

    def quiesce(self, saved):
        self.event('quiesce')
        self.running = False

    def capture(self, saved):
        if self.running:
            raise AssertionError('source is still writing')
        self.event('capture')
        self.archive.write_bytes(self.data.read_bytes())
        return {'archive_id': saved['archive_id'], 'source': dict(SOURCE),
                'source_generation': 'd' * 32, 'complete_sha256': 'f' * 64,
                'continuity': {'repositories': 1, 'issues': 1, 'pull_requests': 1}}

    def pin(self, saved):
        self.event('pin')

    def check_archive(self, saved):
        self.event('check-archive')
        if self.archive.read_text() != 'independent source state':
            raise RuntimeError('archive differs from independent source oracle')

    def migrate(self, saved):
        journal = json.loads((self.helpers / 'upgrade-state.json').read_text())
        if journal['phase'] != 'migration-started' or self.running:
            raise AssertionError('target started without a durable migration boundary')
        self.data.write_text('independent migrated state')
        self.event('migrate')

    def verify_isolated(self, saved):
        self.event('verify-isolated')
        if self.data.read_text() != 'independent migrated state':
            raise RuntimeError('migration continuity failed')

    def stop_isolated(self, saved):
        self.event('stop-isolated')

    def publish(self, saved):
        self.event('publish')
        self.source.update(self.target)

    def activate(self, saved):
        journal = json.loads((self.helpers / 'upgrade-state.json').read_text())
        if journal['phase'] != 'accepted':
            raise AssertionError('normal admission preceded durable acceptance')
        self.event('activate')
        self.running = True

    def verify_current(self, saved):
        self.event('verify-current')
        if not self.running or self.source['image'] != TARGET['image']:
            raise RuntimeError('target is not ready')

    def abort_source(self, saved):
        self.event('abort-source')
        if self.data.read_text() != 'independent source state':
            raise AssertionError('old image would open migrated state')
        self.running = True


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.upgrade = fixtures.load('upgrade')
        root = fixtures.ROOT / '.tmp/forgejo'
        root.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(scratch.cleanup)
        self.host = Host(Path(scratch.name))
        self.evidence = {'schema': 1, 'source': SOURCE, 'target': TARGET,
            'controller_sha256': self.host.controller_sha256,
            'checks': {name: True for name in ('migration', 'continuity', 'source_restore',
                                              'isolation', 'cleanup', 'actions')},
            'cleanup_errors': [], 'completed': True}

    def journal(self):
        return json.loads((self.host.helpers / 'upgrade-state.json').read_text())

    def test_missing_incomplete_or_changed_evidence_rejects_before_stopping(self):
        for changed in ({}, {**self.evidence, 'completed': False},
                        {**self.evidence, 'controller_sha256': 'a' * 64},
                        {**self.evidence, 'cleanup_errors': ['owned cleanup failed']},
                        {**self.evidence, 'target': SOURCE},
                        {**self.evidence, 'checks': {}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.upgrade.perform(self.host, changed)
            self.assertTrue(self.host.running)
            self.assertNotIn('quiesce', self.host.events)
            self.assertFalse((self.host.helpers / 'upgrade-state.json').exists())

    def test_success_keeps_source_stopped_and_accepts_before_normal_admission(self):
        result = self.upgrade.perform(self.host, self.evidence)
        self.assertTrue(result['changed'])
        self.assertEqual('committed', self.journal()['phase'])
        self.assertTrue(self.host.running)
        self.assertEqual('independent source state', self.host.archive.read_text())
        self.assertLess(self.host.events.index('pin'), self.host.events.index('capture'))
        self.assertLess(self.host.events.index('check-archive'), self.host.events.index('migrate'))
        self.assertLess(self.host.events.index('verify-isolated'), self.host.events.index('publish'))

    def test_failure_after_migration_never_restarts_source_or_replaces_archive(self):
        self.host.fail = 'verify-isolated'
        with self.assertRaises(RuntimeError):
            self.upgrade.perform(self.host, self.evidence)
        self.assertEqual('migration-started', self.journal()['phase'])
        self.assertFalse(self.host.running)
        self.assertEqual('independent migrated state', self.host.data.read_text())
        self.assertEqual('independent source state', self.host.archive.read_text())
        with self.assertRaises(RuntimeError):
            self.upgrade.abort(self.host)
        self.assertNotIn('abort-source', self.host.events)

    def test_resume_reuses_source_archive_and_only_runs_the_same_target(self):
        self.host.fail = 'verify-isolated'
        with self.assertRaises(RuntimeError):
            self.upgrade.perform(self.host, self.evidence)
        saved = self.journal()
        self.host.fail = None
        self.upgrade.resume(self.host, self.evidence)
        self.assertEqual(saved['archive_id'], self.journal()['archive_id'])
        self.assertEqual(1, self.host.events.count('capture'))
        self.assertEqual('committed', self.journal()['phase'])

    def test_explicit_pre_migration_abort_restores_only_unmigrated_source(self):
        self.host.fail = 'capture'
        with self.assertRaises(RuntimeError):
            self.upgrade.perform(self.host, self.evidence)
        self.assertFalse(self.host.running)
        self.host.fail = None
        self.upgrade.abort(self.host)
        self.assertTrue(self.host.running)
        self.assertEqual('aborted', self.journal()['phase'])
        self.assertNotIn('migrate', self.host.events)

    def test_repeat_completed_transition_is_observational(self):
        self.upgrade.perform(self.host, self.evidence)
        events = len(self.host.events)
        result = self.upgrade.perform(self.host, self.evidence)
        self.assertFalse(result['changed'])
        self.assertEqual(['verify-current'], self.host.events[events:])

    def test_preparation_gates_admission_without_stopping_or_migrating_source(self):
        result = self.upgrade.perform(self.host, self.evidence, prepare_only=True)
        self.assertEqual('prepared', result['phase'])
        self.assertTrue(self.host.running)
        self.assertIn('block-admission', self.host.events)
        self.assertNotIn('quiesce', self.host.events)
        self.assertNotIn('migrate', self.host.events)

    def test_activation_failure_keeps_durable_target_acceptance_and_can_resume(self):
        self.host.fail = 'activate'
        with self.assertRaises(RuntimeError):
            self.upgrade.perform(self.host, self.evidence)
        self.assertEqual('accepted', self.journal()['phase'])
        self.assertFalse(self.host.running)
        with self.assertRaises(RuntimeError):
            self.upgrade.abort(self.host)
        self.host.fail = None
        self.upgrade.resume(self.host, self.evidence)
        self.assertEqual(1, self.host.events.count('capture'))
        self.assertEqual(1, self.host.events.count('migrate'))
        self.assertEqual('committed', self.journal()['phase'])
