"""Reject incomplete and unsafe archives using independent fixture bytes."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / 'roles/forgejo/files'


def load(name):
    path = FILES / (name + '.py')
    if not path.exists():
        raise AssertionError(name + ' implementation is missing')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.archive = load('archive')
        root = ROOT / '.tmp/forgejo'
        root.mkdir(parents=True, exist_ok=True)
        self.scratch = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / 'forgejo-20260930T033000Z-abcdef12'
        self.path.mkdir(mode=0o700)
        self.manifest = {'schema': 1, 'archive_id': self.path.name,
            'created_at': '2026-09-30T03:30:00Z', 'postgres_major': 17,
            'image': 'codeberg.org/forgejo/forgejo:15.0.9-rootless@sha256:' + 'a' * 64,
            'postgres_image': 'docker.io/library/postgres:17.11@sha256:' + 'b' * 64,
            'rclone_image': 'docker.io/rclone/rclone:1.75.1@sha256:' + 'c' * 64,
            'layout': {'data': '/var/lib/gitea', 'config': '/etc/gitea'},
            'recovery_generation': 'd' * 32}
        (self.path / 'database.dump').write_bytes(b'PGDMPfixture')
        self.write_tar('data/git/repositories/fixture.git/HEAD', b'ref: refs/heads/main\n')
        self.finish()

    def write_tar(self, name, content, *, link=None):
        with tarfile.open(self.path / 'files.tar.gz', 'w:gz') as stream:
            item = tarfile.TarInfo(name)
            item.uid = item.gid = 1000
            item.mode = 0o600
            if link is None:
                item.size = len(content)
                stream.addfile(item, io.BytesIO(content))
            else:
                item.type = tarfile.SYMTYPE
                item.linkname = link
                stream.addfile(item)

    def finish(self):
        (self.path / 'manifest.json').write_text(json.dumps(self.manifest))
        names = ['database.dump', 'files.tar.gz', 'manifest.json']
        (self.path / 'SHA256SUMS').write_text(''.join(
            hashlib.sha256((self.path / name).read_bytes()).hexdigest() + '  ' + name + '\n'
            for name in names))
        for item in self.path.iterdir():
            item.chmod(0o600)

    def test_valid_local_archive_returns_metadata(self):
        self.assertEqual(self.manifest, self.archive.validate_archive(self.path, False))

    def test_remote_requires_complete(self):
        with self.assertRaises((OSError, ValueError)):
            self.archive.validate_archive(self.path, True)

    def test_completion_binds_all_four_files(self):
        complete = self.archive.completion(self.path)
        self.assertEqual({'database.dump', 'files.tar.gz', 'manifest.json', 'SHA256SUMS'},
                         set(complete['files']))
        (self.path / 'COMPLETE').write_text(json.dumps(complete))
        (self.path / 'COMPLETE').chmod(0o600)
        self.assertEqual(self.manifest, self.archive.validate_archive(self.path, True))
        complete['files']['files.tar.gz']['size'] += 1
        (self.path / 'COMPLETE').write_text(json.dumps(complete))
        with self.assertRaises(ValueError):
            self.archive.validate_archive(self.path, True)

    def test_corruption_is_rejected(self):
        (self.path / 'database.dump').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.archive.validate_archive(self.path, False)

    def test_tar_traversal_and_links_are_rejected(self):
        for name, link in [('data/../../outside', None), ('/data/outside', None),
                           ('data/link', '../../outside')]:
            with self.subTest(name=name):
                self.write_tar(name, b'fixture', link=link)
                self.finish()
                with self.assertRaises(ValueError):
                    self.archive.validate_archive(self.path, False)

    def test_noncanonical_tar_path_is_rejected(self):
        self.write_tar('data/./file', b'fixture')
        self.finish()
        with self.assertRaises(ValueError):
            self.archive.validate_archive(self.path, False)

    def test_unknown_payload_and_symlink_are_rejected(self):
        (self.path / 'extra').write_text('unexpected')
        with self.assertRaises(ValueError):
            self.archive.validate_archive(self.path, False)
        (self.path / 'extra').unlink()
        (self.path / 'database.dump').unlink()
        (self.path / 'database.dump').symlink_to('/dev/null')
        with self.assertRaises(ValueError):
            self.archive.validate_archive(self.path, False)

    def test_incompatible_manifest_is_rejected(self):
        self.manifest['postgres_major'] = 16
        self.finish()
        with self.assertRaises(ValueError):
            self.archive.validate_archive(self.path, False)

    def test_hardlinked_payload_is_rejected(self):
        os.link(self.path / 'database.dump', self.path.parent / 'outside')
        with self.assertRaises(ValueError):
            self.archive.validate_archive(self.path, False)

    def test_too_many_empty_members_are_rejected(self):
        with tarfile.open(self.path / 'files.tar.gz', 'w:gz') as stream:
            for number in range(4):
                item = tarfile.TarInfo('data/empty-' + str(number))
                item.uid = item.gid = 1000
                stream.addfile(item)
        with patch.object(self.archive, 'MAX_MEMBERS', 3, create=True):
            with self.assertRaisesRegex(ValueError, 'limit'):
                self.archive.validate_tar(self.path / 'files.tar.gz')

    def test_expanded_content_limit_rejects_compressible_payload(self):
        self.write_tar('data/large', b'x' * 4096)
        with patch.object(self.archive, 'MAX_EXPANDED_BYTES', 3072, create=True):
            with self.assertRaisesRegex(ValueError, 'limit'):
                self.archive.validate_tar(self.path / 'files.tar.gz')

    def test_decompressed_stream_limit_rejects_metadata_expansion(self):
        with patch.object(self.archive, 'MAX_STREAM_BYTES', 1024, create=True):
            with self.assertRaisesRegex(ValueError, 'limit'):
                self.archive.validate_tar(self.path / 'files.tar.gz')


if __name__ == '__main__':
    unittest.main()
