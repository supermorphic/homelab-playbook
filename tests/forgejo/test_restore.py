"""Restore rejects incompatible inputs before creating runtime resources."""
from dataclasses import replace
import importlib
import io
import json
from pathlib import Path
import tarfile
import unittest
from unittest.mock import patch

import test_archive as fixtures


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((fixtures.ROOT / 'scripts/forgejo/restore.py').exists(),
                        'exact archive restore is missing')
        self.restore = importlib.import_module('scripts.forgejo.restore')
        self.fixture = fixtures.ArchiveTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.path
        self.settings = self.path.parent / 'settings'
        self.settings.mkdir(mode=0o700)
        self.keys = ('database-password', 'secret-key', 'internal-token',
                     'lfs-jwt-secret', 'oauth2-jwt-secret')
        for name in self.keys:
            path = self.settings / name
            path.write_text('independent-fixture-' + name)
            path.chmod(0o600)
        generation = self.settings / 'generation.json'
        generation.write_text(json.dumps({'schema': 1,
            'recovery_generation': self.fixture.manifest['recovery_generation']}))
        generation.chmod(0o600)
        with tarfile.open(self.path / 'files.tar.gz', 'w:gz') as stream:
            contents = {'data/git/repositories/fixture.git/HEAD': b'ref: refs/heads/main\n',
                        'config/app.ini': b'[database]\nDB_TYPE=postgres\n'}
            contents.update({'config/' + name: (self.settings / name).read_bytes() for name in self.keys})
            for name, content in contents.items():
                item = tarfile.TarInfo(name)
                item.uid = item.gid = 1000
                item.mode = 0o600
                item.size = len(content)
                stream.addfile(item, io.BytesIO(content))
        self.fixture.finish()
        marker = self.path / 'COMPLETE'
        marker.write_text(json.dumps(self.fixture.archive.completion(self.path)))
        marker.chmod(0o600)
        self.pins = {key: self.fixture.manifest[key] for key in ('image', 'postgres_image', 'rclone_image')}
        self.destination = self.path.parent / 'new-restore'

    def preflight(self):
        return self.restore.preflight_archive(self.path, self.settings, self.pins, self.destination)

    def test_valid_archive_and_matching_settings_are_accepted_without_mutation(self):
        self.assertEqual(self.fixture.manifest, self.preflight())
        self.assertFalse(self.destination.exists())

    def test_missing_complete_is_rejected_before_destination_creation(self):
        (self.path / 'COMPLETE').unlink()
        with self.assertRaises((OSError, ValueError)):
            self.preflight()
        self.assertFalse(self.destination.exists())

    def test_wrong_image_is_rejected(self):
        self.pins['image'] = self.pins['image'].replace('15.0.9', '15.0.8')
        with self.assertRaises(ValueError):
            self.preflight()
        self.assertFalse(self.destination.exists())

    def test_wrong_generation_is_rejected(self):
        (self.settings / 'generation.json').write_text(json.dumps({'schema': 1,
            'recovery_generation': 'e' * 32}))
        with self.assertRaises(ValueError):
            self.preflight()

    def test_wrong_key_is_rejected_before_startup(self):
        (self.settings / 'secret-key').write_text('incorrect-fixture-value')
        with self.assertRaises(ValueError):
            self.preflight()
        self.assertFalse(self.destination.exists())

    def test_occupied_destination_is_preserved(self):
        self.destination.mkdir()
        sentinel = self.destination / 'keep'
        sentinel.write_text('independent sentinel')
        with self.assertRaises(ValueError):
            self.preflight()
        self.assertEqual('independent sentinel', sentinel.read_text())

    def test_parent_traversal_is_rejected_before_retrieval(self):
        destination = fixtures.ROOT / '.tmp/forgejo/../../../outside/new-restore'
        rclone = self.settings / 'rclone.conf'
        rclone.write_text('[nas]\ntype = smb\n')
        rclone.chmod(0o600)
        expected = self.settings / 'expected.json'
        expected.write_text(json.dumps({'schema': 1, 'username': 'fixture', 'repository': 'fixture',
            'credential_file': str(self.settings / 'secret-key'), 'refs': {'refs/heads/main': 'a' * 40},
            'issue': {'id': 1}, 'pull_request': {'id': 1}, 'comment': {'id': 1},
            'attachment': {'id': 1, 'sha256': 'b' * 64},
            'lfs': {'path': 'fixture.bin', 'sha256': 'c' * 64}}))
        expected.chmod(0o600)
        options = self.restore.Options(self.path.name, 'nas:fixture',
            rclone, self.settings, expected,
            destination, 'abcdef1234567890', 'abcdef1234567890')
        with self.assertRaisesRegex(ValueError, 'restore destination'):
            self.restore.validate_options(options)
        self.assertFalse(destination.exists())

    def test_destination_is_rechecked_before_creation(self):
        self.destination = self.path.parent / '..' / self.path.parent.name / 'new-restore'
        with self.assertRaisesRegex(ValueError, 'restore destination'):
            self.preflight()
        self.assertFalse(self.destination.exists())

    def test_insufficient_expansion_capacity_is_rejected(self):
        with patch.object(self.restore.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 0})()):
            with self.assertRaises(ValueError):
                self.preflight()
        self.assertFalse(self.destination.exists())

    def test_capacity_accounts_for_empty_entries_and_parent_directories(self):
        with tarfile.open(self.path / 'files.tar.gz', 'r:gz') as source:
            members = [(item, source.extractfile(item).read()) for item in source if item.isfile()]
        with tarfile.open(self.path / 'files.tar.gz', 'w:gz') as stream:
            for item, content in members:
                stream.addfile(item, io.BytesIO(content))
            for number in range(128):
                item = tarfile.TarInfo('data/empty/' + str(number))
                item.uid = item.gid = 1000
                stream.addfile(item)
        self.fixture.finish()
        (self.path / 'COMPLETE').write_text(json.dumps(self.fixture.archive.completion(self.path)))
        available = 256 * 1024 * 1024 + 4096
        with patch.object(self.restore.shutil, 'disk_usage', return_value=type('Usage', (), {'free': available})()):
            with self.assertRaisesRegex(ValueError, 'capacity'):
                self.preflight()
        self.assertFalse(self.destination.exists())

    def test_corruption_is_rejected_before_startup(self):
        (self.path / 'database.dump').write_bytes(b'PGDMPcorrupted')
        with self.assertRaises(ValueError):
            self.preflight()

    def test_special_file_is_rejected(self):
        with tarfile.open(self.path / 'files.tar.gz', 'w:gz') as stream:
            item = tarfile.TarInfo('data/special')
            item.type = tarfile.FIFOTYPE
            item.uid = item.gid = 1000
            stream.addfile(item)
        self.fixture.finish()
        (self.path / 'COMPLETE').write_text(json.dumps(self.fixture.archive.completion(self.path)))
        with self.assertRaises(ValueError):
            self.preflight()

    def test_lfs_action_forwards_protocol_accept_and_private_authorization(self):
        self.assertIn('Basic Zml4dHVyZTpteQ==', self.restore.lfs_config('', {
            'Authorization': 'Basic Zml4dHVyZTpteQ=='}))
        content = self.restore.lfs_config('user = "fixture:synthetic"\n', {
            'Authorization': 'Bearer fixture.token.signature', 'Accept': 'application/vnd.git-lfs'})
        self.assertIn('header = "Accept: application/vnd.git-lfs"', content)
        self.assertIn('header = "Authorization: Bearer fixture.token.signature"', content)
        with self.assertRaises(ValueError):
            self.restore.lfs_config('', {'Authorization': 'Bearer fixture\nextra'})

    def test_cleanup_preserves_uncreated_destination_with_matching_marker(self):
        self.destination.mkdir(mode=0o700)
        marker = self.destination / 'ownership.json'
        marker.write_text(json.dumps({'run_id': 'abcdef1234567890'}))
        marker.chmod(0o600)
        sentinel = self.destination / 'keep'
        sentinel.write_text('independent sentinel')
        self.restore.cleanup_destination(self.destination, {}, 'abcdef1234567890')
        self.assertEqual('independent sentinel', sentinel.read_text())

    def test_lfs_pointer_oracle_rejects_different_oid(self):
        lfs = {'sha256': 'a' * 64, 'size': 42}
        pointer = 'version https://git-lfs.github.com/spec/v1\noid sha256:' + 'a' * 64 + '\nsize 42\n'
        self.restore.assert_lfs_pointer(pointer, lfs)
        with self.assertRaises(RuntimeError):
            self.restore.assert_lfs_pointer(pointer.replace('a' * 64, 'b' * 64), lfs)

    def test_isolation_arguments_have_no_egress_or_host_listener(self):
        args = self.restore.application_arguments('owned-database', Path('/synthetic/data'),
            Path('/synthetic/config'), Path('/synthetic/auth'), self.pins['image'])
        self.assertEqual('container:owned-database', args[args.index('--network') + 1])
        self.assertNotIn('--publish', args)
        self.assertNotIn('--privileged', args)
        self.assertFalse(any('/var/lib/svc-forgejo' in value for value in args))
        database = self.restore.database_arguments(Path('/synthetic/database.env'), 'owned-volume', self.pins['postgres_image'])
        self.assertEqual('none', database[database.index('--network') + 1])
        self.assertNotIn('--publish', database)

    def test_application_shares_database_uid_mapping_for_private_mounts(self):
        args = self.restore.application_arguments('owned-database', Path('/synthetic/data'),
            Path('/synthetic/config'), Path('/synthetic/auth'), self.pins['image'])
        self.assertIn('--userns', args)
        self.assertEqual('container:owned-database', args[args.index('--userns') + 1])
        database = self.restore.database_arguments(Path('/synthetic/database.env'),
            'owned-volume', self.pins['postgres_image'])
        self.assertIn('--userns=keep-id:uid=1000,gid=1000', database)


if __name__ == '__main__':
    unittest.main()
