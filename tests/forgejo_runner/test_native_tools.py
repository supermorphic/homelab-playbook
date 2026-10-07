"""Public native helpers must keep their exact identity and private scope."""

import gzip
import hashlib
import importlib
import io
from pathlib import Path
import stat
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


class NativeToolTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('scripts.forgejo_runner.native_tools')
        self.binary = bytearray(64)
        self.binary[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', self.binary, 18, 62)
        self.binary = bytes(self.binary)
        self.compressed = gzip.compress(self.binary, mtime=0)
        self.pin = {
            'version': '1.16.1', 'architecture': 'amd64',
            'url': 'https://example.invalid/netavark.gz',
            'compressed_size': len(self.compressed),
            'compressed_sha256': hashlib.sha256(self.compressed).hexdigest(),
            'size': len(self.binary), 'sha256': hashlib.sha256(self.binary).hexdigest(),
        }
        self.pinned = patch.object(self.module, 'NETAVARK', self.pin)
        self.pinned.start()
        self.addCleanup(self.pinned.stop)

    def archive(self, path, data=None, *, extra=False, link=False):
        with tarfile.open(path, 'w') as archive:
            info = tarfile.TarInfo('netavark')
            if link:
                info.type = tarfile.SYMTYPE
                info.linkname = '/etc/passwd'
                archive.addfile(info)
            else:
                data = self.binary if data is None else data
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            if extra:
                archive.addfile(tarfile.TarInfo('unexpected'))

    def test_preparation_checks_download_and_native_abi_before_staging(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.module.urllib.request, 'urlopen',
                             return_value=io.BytesIO(self.compressed)):
            path = self.module.prepare_archive(Path(directory), 'amd64')
            with tarfile.open(path) as archive:
                self.assertEqual(['netavark'], archive.getnames())
                self.assertEqual(self.binary, archive.extractfile('netavark').read())

    def test_bad_download_does_not_create_an_archive(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.module.urllib.request, 'urlopen',
                             return_value=io.BytesIO(b'counterfeit')):
            with self.assertRaises(ValueError):
                self.module.prepare_archive(Path(directory), 'amd64')
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_wrong_native_abi_is_rejected_even_with_matching_hashes(self):
        binary = bytearray(self.binary)
        struct.pack_into('<H', binary, 18, 183)
        compressed = gzip.compress(bytes(binary), mtime=0)
        self.pin.update(compressed_size=len(compressed),
                        compressed_sha256=hashlib.sha256(compressed).hexdigest(),
                        sha256=hashlib.sha256(binary).hexdigest())
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.module.urllib.request, 'urlopen',
                             return_value=io.BytesIO(compressed)):
            with self.assertRaises(ValueError):
                self.module.prepare_archive(Path(directory), 'amd64')
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_installation_creates_only_the_verified_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, executable = root/'input.tar', root/'netavark'
            self.archive(archive)
            self.module.install_archive(archive, executable)
            self.assertEqual(self.binary, executable.read_bytes())
            self.assertEqual(0o555, stat.S_IMODE(executable.stat().st_mode))

    def test_bad_archive_never_creates_an_executable(self):
        for variant in ('extra', 'link', 'checksum'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive, executable = root/'input.tar', root/'netavark'
                self.archive(archive, b'x'*64 if variant=='checksum' else None,
                             extra=variant=='extra', link=variant=='link')
                with self.assertRaises(ValueError):
                    self.module.install_archive(archive, executable)
                self.assertFalse(executable.exists())

    def test_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, executable = root/'input.tar', root/'netavark'
            self.archive(archive)
            executable.write_bytes(b'other allocation')
            with self.assertRaises(FileExistsError):
                self.module.install_archive(archive, executable)
            self.assertEqual(b'other allocation', executable.read_bytes())

    def test_archive_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.archive(root/'real.tar')
            (root/'input.tar').symlink_to(root/'real.tar')
            with self.assertRaises((OSError, ValueError)):
                self.module.install_archive(root/'input.tar', root/'netavark')
            self.assertFalse((root/'netavark').exists())

    def test_version_check_requires_the_pinned_executable_to_run(self):
        for output in ('netavark 1.16.1\n', 'netavark 1.14.0\n', ''):
            with self.subTest(output=output), patch.object(
                    self.module.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, output, '')) as run:
                if output == 'netavark 1.16.1\n':
                    self.module.verify_version()
                else:
                    with self.assertRaises(ValueError):
                        self.module.verify_version()
                run.assert_called_once_with(['/etc/runner-tools/netavark', '--version'],
                                            capture_output=True, text=True, check=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
