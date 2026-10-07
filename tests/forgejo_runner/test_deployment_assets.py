import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import json
import subprocess

source = Path(__file__).parents[2] / 'scripts/forgejo_runner/deployment_assets.py'
spec = importlib.util.spec_from_file_location('deployment_assets', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AssetTests(unittest.TestCase):
    def test_cached_image_uses_independently_verified_digest_and_architecture(self):
        pin = 'quay.io/podman/stable@sha256:' + 'a'*64
        run = Mock(podman='podman')
        run.command.side_effect = [subprocess.CompletedProcess([], 0, ''),
            subprocess.CompletedProcess([], 0, json.dumps([{
                'Architecture': 'amd64', 'RepoDigests': [pin], 'Id': 'b'*64}]))]
        self.assertEqual('b'*64, module.ensure_image(run, pin, Path('/synthetic-auth.json')))
        self.assertFalse(any(call.args[0][1] == 'pull' for call in run.command.call_args_list))

    def test_missing_image_is_pulled_with_empty_auth_then_verified(self):
        pin = 'quay.io/podman/stable@sha256:' + 'a'*64
        run = Mock(podman='podman')
        run.command.side_effect = [subprocess.CompletedProcess([], 1, ''),
            subprocess.CompletedProcess([], 0, ''),
            subprocess.CompletedProcess([], 0, json.dumps([{
                'Architecture': 'amd64', 'RepoDigests': [pin], 'Id': 'b'*64}]))]
        self.assertEqual('b'*64, module.ensure_image(run, pin, Path('/synthetic-auth.json')))
        self.assertEqual(['podman', 'pull', '--authfile', '/synthetic-auth.json', '--arch=amd64', pin],
                         run.command.call_args_list[1].args[0])

    def test_cached_wrong_architecture_or_digest_is_rejected_without_replacement(self):
        pin = 'quay.io/podman/stable@sha256:' + 'a'*64
        for architecture, digests in (('arm64', [pin]), ('amd64', [])):
            run = Mock(podman='podman')
            run.command.side_effect = [subprocess.CompletedProcess([], 0, ''),
                subprocess.CompletedProcess([], 0, json.dumps([{
                    'Architecture': architecture, 'RepoDigests': digests, 'Id': 'b'*64}]))]
            with self.subTest(architecture=architecture, digests=digests), self.assertRaises(ValueError):
                module.ensure_image(run, pin, Path('/synthetic-auth.json'))
            self.assertFalse(any(call.args[0][1] == 'pull' for call in run.command.call_args_list))

    def test_manifest_measures_actual_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / 'job.oci'
            artifact.write_bytes(b'independent-payload')
            result = module.describe(artifact)
            import hashlib
            self.assertEqual(len(b'independent-payload'), result['size'])
            self.assertEqual(hashlib.sha256(b'independent-payload').hexdigest(), result['sha256'])
            module.verify(artifact, result)
            artifact.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                module.verify(artifact, result)

    def test_linked_and_oversize_artifacts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / 'file'
            artifact.write_bytes(b'public')
            for link in ('symbolic', 'hard'):
                path = root / link
                if link == 'hard':
                    os.link(artifact, path)
                else:
                    path.symlink_to(artifact)
                with self.assertRaises((ValueError, OSError)):
                    module.describe(path)
            (root / 'hard').unlink()
            with self.assertRaises(ValueError):
                module.describe(artifact, maximum=2)

    def test_only_amd64_elf_is_promoted_as_controller_executable(self):
        elf = bytearray(64)
        elf[:7] = b'\x7fELF\x02\x01\x01'
        elf[16:20] = b'\x02\x00\x3e\x00'
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'forgejo-runner'
            executable.write_bytes(elf)
            module.validate_executable(executable)
            elf[18:20] = b'\xb7\x00'
            executable.write_bytes(elf)
            with self.assertRaises(ValueError):
                module.validate_executable(executable)


if __name__ == '__main__':
    unittest.main()
