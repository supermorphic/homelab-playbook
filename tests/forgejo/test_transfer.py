"""Backlog, completion and retention behavior against a separate filesystem."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest

import test_archive as fixtures
sys.path.insert(0, str(fixtures.FILES))


class Remote:
    def __init__(self, root):
        self.root = root
        root.mkdir(mode=0o700)
        self.fail_check = False
        self.fail_upload = False
        self.payload_downloads = 0
        self.uploads = 0
    def listing(self, identifier=None):
        path = self.root / identifier if identifier else self.root
        if not path.exists():
            return {}
        return {p.name: {'size': p.stat().st_size, 'directory': p.is_dir(),
                         'link': p.is_symlink()} for p in path.iterdir()}
    def read(self, identifier, name):
        if name in ('database.dump', 'files.tar.gz'):
            self.payload_downloads += 1
        return (self.root / identifier / name).read_bytes()
    def copy(self, source, identifier):
        self.uploads += 1
        path = self.root / identifier
        path.mkdir(exist_ok=True, mode=0o700)
        for name in ('database.dump', 'files.tar.gz', 'manifest.json', 'SHA256SUMS'):
            shutil.copyfile(source / name, path / name)
            (path / name).chmod(0o600)
            if self.fail_upload:
                raise RuntimeError('synthetic outage')
    def check(self, source, identifier):
        if self.fail_check:
            raise RuntimeError('synthetic verification failure')
        for name in ('database.dump', 'files.tar.gz', 'manifest.json', 'SHA256SUMS'):
            if (source / name).read_bytes() != self.read(identifier, name):
                raise RuntimeError('synthetic mismatch')
    def publish(self, identifier, value):
        path = self.root / identifier / 'COMPLETE'
        path.write_bytes(value)
        path.chmod(0o600)
    def remove_file(self, identifier, name):
        (self.root / identifier / name).unlink()
    def remove_directory(self, identifier):
        (self.root / identifier).rmdir()


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.transfer = fixtures.load('transfer')
        self.fixture = fixtures.ArchiveTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backups = self.fixture.path.parent / 'backups'
        self.backups.mkdir(mode=0o700)
        self.path = self.backups / self.fixture.path.name
        self.fixture.path.rename(self.path)
        self.fixture.path = self.path
        self.remote = Remote(self.backups.parent / 'remote')
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    def run_transfer(self):
        return self.transfer.transfer(self.backups, self.remote, now=self.now)
    def new_archive(self, timestamp, suffix):
        identifier = 'forgejo-' + timestamp + '-' + suffix
        path = self.backups / identifier
        shutil.copytree(self.path, path)
        path.chmod(0o700)
        manifest = json.loads((path / 'manifest.json').read_text())
        manifest['archive_id'] = identifier
        manifest['created_at'] = datetime.strptime(timestamp, '%Y%m%dT%H%M%SZ').strftime('%Y-%m-%dT%H:%M:%SZ')
        (path / 'manifest.json').write_text(json.dumps(manifest))
        (path / 'SHA256SUMS').write_text(''.join(hashlib.sha256((path / name).read_bytes()).hexdigest() + '  ' + name + '\n'
            for name in ('database.dump', 'files.tar.gz', 'manifest.json')))
        return path

    def test_check_failure_never_publishes_complete_and_preserves_local(self):
        self.remote.fail_check = True
        self.assertTrue(self.run_transfer()['errors'])
        self.assertFalse((self.remote.root / self.path.name / 'COMPLETE').exists())
        self.assertTrue(self.path.is_dir())

    def test_partial_upload_recovers_without_losing_local_archive(self):
        self.remote.fail_upload = True
        self.assertTrue(self.run_transfer()['errors'])
        self.remote.fail_upload = False
        self.assertFalse(self.run_transfer()['errors'])
        self.assertTrue((self.remote.root / self.path.name / 'COMPLETE').is_file())

    def test_unchanged_run_reads_only_metadata(self):
        self.assertFalse(self.run_transfer()['errors'])
        downloads, uploads = self.remote.payload_downloads, self.remote.uploads
        self.assertFalse(self.run_transfer()['errors'])
        self.assertEqual(downloads, self.remote.payload_downloads)
        self.assertEqual(uploads, self.remote.uploads)

    def test_three_archive_outage_backlog_is_retried(self):
        self.new_archive('20260929T033000Z', 'abcdef13')
        self.new_archive('20260928T033000Z', 'abcdef14')
        self.remote.fail_upload = True
        self.assertEqual(3, len(self.run_transfer()['errors']))
        self.remote.fail_upload = False
        self.assertFalse(self.run_transfer()['errors'])
        self.assertEqual(3, len(self.remote.listing()))

    def test_mismatched_complete_is_rejected_without_overwrite(self):
        self.run_transfer()
        marker = self.remote.root / self.path.name / 'COMPLETE'
        body = json.loads(marker.read_text())
        body['files']['files.tar.gz']['size'] += 1
        marker.write_text(json.dumps(body))
        before = marker.read_bytes()
        self.assertTrue(self.run_transfer()['errors'])
        self.assertEqual(before, marker.read_bytes())

    def test_local_retention_requires_verified_remote(self):
        self.now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        self.remote.fail_upload = True
        self.assertTrue(self.run_transfer()['errors'])
        self.assertTrue(self.path.exists())
        self.remote.fail_upload = False
        self.assertFalse(self.run_transfer()['errors'])
        self.assertFalse(self.path.exists())
        self.assertTrue((self.remote.root / self.path.name / 'COMPLETE').exists())

    def test_expired_untransferred_backlog_requires_operator_resolution(self):
        self.now = datetime(2027, 1, 1, tzinfo=timezone.utc)
        self.assertTrue(self.run_transfer()['errors'])
        self.assertTrue(self.path.exists())
        self.assertEqual(0, self.remote.uploads)

    def test_remote_retention_removes_only_valid_complete_generations(self):
        self.run_transfer()
        self.now = datetime(2027, 1, 1, tzinfo=timezone.utc)
        self.assertFalse(self.run_transfer()['errors'])
        self.assertFalse((self.remote.root / self.path.name).exists())
        self.assertFalse(self.path.exists())

    def test_forged_names_and_links_are_preserved(self):
        bad = self.backups / 'not-an-archive'
        bad.mkdir()
        (bad / 'keep').write_text('independent sentinel')
        (self.backups / 'forgejo-20260930T033000Z-abcdef15').symlink_to(bad, target_is_directory=True)
        self.assertTrue(self.run_transfer()['errors'])
        self.assertEqual('independent sentinel', (bad / 'keep').read_text())


if __name__ == '__main__':
    unittest.main()
