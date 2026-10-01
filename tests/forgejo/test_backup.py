"""Inject capture/recovery failures at command boundaries, preserving real state."""
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from test_archive import ROOT, FILES, load
sys.path.insert(0, str(FILES))


class Host:
    def __init__(self, root):
        self.state = root / 'state'
        self.state.mkdir(mode=0o700)
        self.backups = self.state / 'backups'
        self.backups.mkdir(mode=0o700)
        self.lock = root / 'lock'
        self.lock.touch(mode=0o600)
        self.settings = {'recovery_generation': 'd' * 32,
            'image': 'codeberg.org/forgejo/forgejo:15.0.9-rootless@sha256:' + 'a' * 64,
            'postgres_image': 'docker.io/library/postgres:17.11@sha256:' + 'b' * 64,
            'rclone_image': 'docker.io/rclone/rclone:1.75.1@sha256:' + 'c' * 64}
        self.running = True
        self.fail = None
        self.events = []
    def preflight(self):
        self.events.append('preflight')
    def active(self):
        return self.running
    def stop(self):
        self.events.append('stop')
        self.running = False
        if self.fail == 'interrupt':
            raise KeyboardInterrupt()
    def stopped(self):
        if self.running:
            raise RuntimeError('still writing')
    def dump(self, path):
        self.stopped()
        self.events.append('dump-stopped')
        if self.fail in ('dump', 'timeout'):
            raise RuntimeError(self.fail + ' failed')
        path.write_bytes(b'PGDMPindependent-fixture')
        path.chmod(0o600)
    def files(self, path):
        self.stopped()
        self.events.append('files-stopped')
        if self.fail == 'files':
            raise RuntimeError('files failed')
        with tarfile.open(path, 'w:gz') as stream:
            item = tarfile.TarInfo('data/git/repositories/fixture.git/HEAD')
            content = b'ref: refs/heads/main\n'
            item.uid = item.gid = 1000
            item.mode = 0o600
            item.size = len(content)
            stream.addfile(item, io.BytesIO(content))
        path.chmod(0o600)
    def readable_dump(self, path):
        self.events.append('readable')
    def start(self):
        self.events.append('start')
        if self.fail == 'restart':
            raise RuntimeError('restart failed')
        self.running = True
    def health(self):
        if not self.running:
            raise RuntimeError('unhealthy')
    def trigger_transfer(self):
        self.events.append('transfer')


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.backup = load('backup')
        root = ROOT / '.tmp/forgejo'
        root.mkdir(parents=True, exist_ok=True)
        self.scratch = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.scratch.cleanup)
        self.host = Host(Path(self.scratch.name))

    def test_success_publishes_only_stopped_capture_then_recovers(self):
        result = self.backup.perform_capture(self.host)
        self.assertTrue(self.host.running)
        self.assertEqual(['preflight', 'stop', 'dump-stopped', 'files-stopped',
                          'readable', 'start', 'transfer'], self.host.events)
        path = self.host.backups / result['archive_id']
        self.assertTrue((path / 'SHA256SUMS').is_file())
        self.assertFalse((self.host.state / 'restart-intent.json').exists())

    def test_dump_file_and_timeout_failures_remain_visible_after_recovery(self):
        for failure in ('dump', 'files', 'timeout'):
            with self.subTest(failure=failure):
                self.host.fail = failure
                with self.assertRaisesRegex(RuntimeError, failure):
                    self.backup.perform_capture(self.host)
                self.assertTrue(self.host.running)
                self.assertFalse(any(not path.name.startswith('.') for path in self.host.backups.iterdir()))
                self.assertNotIn('transfer', self.host.events)
                self.host.events.clear()

    def test_deliberately_stopped_application_is_not_started_or_backed_up(self):
        self.host.running = False
        self.assertEqual({'skipped': 'application-inactive'}, self.backup.perform_capture(self.host))
        self.assertEqual(['preflight'], self.host.events)
        self.assertEqual([], list(self.host.backups.iterdir()))

    def test_interruption_leaves_intent_for_supervised_recovery(self):
        self.host.fail = 'interrupt'
        with self.assertRaises(KeyboardInterrupt):
            with self.backup.service.operation_lock(self.host.lock, 1):
                self.backup.capture_locked(self.host)
        self.assertFalse(self.host.running)
        self.assertTrue((self.host.state / 'restart-intent.json').exists())
        self.host.fail = None
        self.backup.recover(self.host)
        self.assertTrue(self.host.running)
        self.host.events.clear()
        self.backup.recover(self.host)
        self.assertEqual([], self.host.events)

    def test_recovery_completes_without_archive_sized_validation(self):
        result = self.backup.capture_locked(self.host)
        self.assertFalse(self.host.running)
        def too_slow(*args):
            raise RuntimeError('archive verification exceeds availability budget')
        with patch.object(self.backup.archive, 'validate_archive', too_slow):
            try:
                self.backup.recover(self.host)
            except RuntimeError as error:
                self.fail('availability recovery repeated archive verification: ' + str(error))
        self.assertTrue(self.host.running)
        self.assertFalse((self.host.state / 'restart-intent.json').exists())
        self.assertIn('transfer', self.host.events)
        self.assertTrue((self.host.backups / result['archive_id'] / 'SHA256SUMS').exists())

    def test_failed_restart_preserves_distinct_recovery_failure(self):
        self.host.fail = 'restart'
        with self.assertRaises(RuntimeError):
            self.backup.perform_capture(self.host)
        self.assertTrue((self.host.state / 'restart-intent.json').exists())
        self.assertTrue((self.host.state / 'recovery-failed').exists())
        with self.assertRaises(RuntimeError):
            self.backup.perform_capture(self.host)
        self.host.fail = None
        self.backup.recover(self.host)
        self.assertFalse((self.host.state / 'recovery-failed').exists())

    def test_lock_contention_does_not_stop_application(self):
        self.host.lock_timeout = 0.01
        with self.backup.service.operation_lock(self.host.lock, 1):
            with self.assertRaises(RuntimeError):
                self.backup.perform_capture(self.host)
        self.assertTrue(self.host.running)
        self.assertEqual([], self.host.events)


if __name__ == '__main__':
    unittest.main()
