"""Offline worker inputs must be bounded and checked before use."""

import hashlib
import importlib
import io
from pathlib import Path
import tempfile
import unittest


class HostInputTests(unittest.TestCase):
    def module(self):
        return importlib.import_module('scripts.forgejo_runner.host_inputs')

    def test_stream_checks_exact_size_and_digest_without_reading_past_asset(self):
        module = self.module()
        data = b'synthetic public asset'
        descriptor = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        stream = io.BytesIO(data + b'next asset')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'asset'
            module.receive_asset(stream, path, descriptor)
            self.assertEqual(data, path.read_bytes())
            self.assertEqual(b'next asset', stream.read())
            self.assertEqual(0o444, path.stat().st_mode & 0o777)

    def test_truncated_or_changed_asset_is_rejected(self):
        module = self.module()
        descriptor = {'size': 8, 'sha256': hashlib.sha256(b'expected').hexdigest()}
        for data in (b'short', b'modified'):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'asset'
                with self.assertRaises(ValueError):
                    module.receive_asset(io.BytesIO(data), path, descriptor)
                self.assertFalse(path.exists(), 'Partial owned asset must be removed')

    def test_failed_asset_writeback_is_not_admitted_or_left_for_use(self):
        from unittest.mock import patch
        module = self.module()
        data = b'verified public asset'
        descriptor = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'asset'
            with patch.object(module.os, 'fsync', side_effect=OSError('synthetic writeback failure')):
                with self.assertRaisesRegex(OSError, 'synthetic writeback failure'):
                    module.receive_asset(io.BytesIO(data), path, descriptor)
            self.assertFalse(path.exists())

    def test_asset_cannot_replace_existing_file_or_follow_symlink(self):
        module = self.module()
        descriptor = {'size': 8, 'sha256': hashlib.sha256(b'expected').hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / 'sentinel'; sentinel.write_text('preserve')
            asset = root / 'asset'; asset.symlink_to(sentinel)
            with self.assertRaises(OSError):
                module.receive_asset(io.BytesIO(b'expected'), asset, descriptor)
            self.assertEqual('preserve', sentinel.read_text())

    def test_manifest_rejects_paths_and_unbounded_or_invalid_lengths(self):
        module = self.module()
        good = {'work/input/source.tar': {'size': 128, 'sha256': 'a' * 64}}
        self.assertEqual(128, module.validate_assets(good, 512))
        for assets in ({'/etc/passwd': good['work/input/source.tar']},
                       {'work/input/../outside': good['work/input/source.tar']},
                       {'work/input/source.tar': {'size': True, 'sha256': 'a' * 64}},
                       {'work/input/source.tar': {'size': 0, 'sha256': 'a' * 64}},
                       {'work/input/source.tar': {'size': 513, 'sha256': 'a' * 64}},
                       {'work/input/source.tar': {'size': 128, 'sha256': 'untrusted'}},
                       {'work/input/source.tar': {'size': 128, 'sha256': 'a' * 64, 'extra': 1}}):
            with self.subTest(assets=assets), self.assertRaises(ValueError):
                module.validate_assets(assets, 512)

    def test_ssh_frames_public_assets_after_bounded_python_source(self):
        from unittest.mock import patch
        import subprocess
        from scripts.forgejo_runner import host_fixture
        from test_host_fixture import target_descriptor
        with tempfile.TemporaryDirectory(dir=host_fixture.ROOT / '.tmp') as directory:
            path = Path(directory) / 'asset'; path.write_bytes(b'public archive')
            captured = {}
            def run(argv, **kwargs):
                captured['argv'] = argv
                stream = kwargs['stdin']; length = int(stream.readline())
                captured['payload'] = stream.read(length)
                captured['asset'] = stream.read()
                return subprocess.CompletedProcess(argv, 0, '{}', '')
            with patch.object(host_fixture.subprocess, 'run', side_effect=run):
                host_fixture.ssh_observation(target_descriptor(), 'print("synthetic")',
                                             input_files=[path])
            self.assertEqual(b'print("synthetic")', captured['payload'])
            self.assertEqual(b'public archive', captured['asset'])
            self.assertIn('sudo -n -- /usr/bin/python3 -c ', captured['argv'][-1])

    def test_ssh_rejects_nonlocal_or_symlinked_inputs_before_transport(self):
        from unittest.mock import patch
        from scripts.forgejo_runner import host_fixture
        from test_host_fixture import target_descriptor
        with tempfile.TemporaryDirectory(dir=host_fixture.ROOT / '.tmp') as directory:
            path = Path(directory) / 'asset'; path.write_bytes(b'public archive')
            link = Path(directory) / 'link'; link.symlink_to(path)
            for invalid in (link, Path('/etc/hosts')):
                with patch.object(host_fixture.subprocess, 'run') as transport:
                    with self.assertRaises((OSError, ValueError)):
                        host_fixture.ssh_observation(target_descriptor(), 'print("synthetic")',
                                                     input_files=[invalid])
                    transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
