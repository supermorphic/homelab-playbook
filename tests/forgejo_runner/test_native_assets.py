"""Offline images must retain their reviewed registry identity and native ABI."""

import importlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class ImageStore:
    """An image engine with separate cached content and registry availability."""

    def __init__(self, cached=True, registry=False, exists_error=False, image_change=None):
        self.references = [
            'quay.io/podman/stable@sha256:' + 'a' * 64,
            'data.forgejo.org/forgejo/runner@sha256:' + 'b' * 64,
            'example.invalid/forgejo@sha256:' + 'c' * 64,
            'example.invalid/postgres@sha256:' + 'd' * 64,
        ]
        self.images = {pin: {'Id': identity * 64, 'Architecture': 'amd64',
                            'RepoDigests': [pin], **(image_change or {})}
                       for pin, identity in zip(self.references, '1234')}
        self.cached = set(self.references) if cached else set()
        self.registry = registry
        self.exists_error = exists_error

    def command(self, argv, *, check=True, **kwargs):
        code, output = 0, ''
        if argv[1:3] == ['image', 'exists']:
            code = 125 if self.exists_error else int(argv[3] not in self.cached)
        elif argv[1:3] == ['image', 'inspect']:
            if argv[3] not in self.cached:
                raise AssertionError('Inspection requires a present image')
            output = json.dumps([self.images[argv[3]]])
        elif argv[1] == 'pull':
            if self.registry:
                self.cached.add(argv[-1])
            else:
                code = 125
        elif argv[1] == 'save':
            if argv[-1] not in {self.images[pin]['Id'] for pin in self.cached}:
                raise AssertionError('Only inspected cached content can be exported')
            Path(argv[argv.index('--output') + 1]).write_bytes(b'public OCI asset')
        else:
            raise AssertionError('Unexpected image operation')
        if check and code:
            raise RuntimeError('Registry manifest unavailable')
        return subprocess.CompletedProcess(argv, code, output, '')


class NativeAssetTests(unittest.TestCase):
    def prepare_assets(self, store):
        module = importlib.import_module('scripts.forgejo_runner.native_assets')
        from scripts.forgejo_runner.fixture import ROOT, RunnerRun
        scratch = ROOT / '.tmp/forgejo-runner'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            directory = Path(directory)
            descriptor = directory / 'candidate.json'
            descriptor.write_text(json.dumps({
                'probe_images': {'amd64': store.references[0]},
                'runner_images': {'amd64': store.references[1]},
            }))
            with patch('scripts.forgejo.runtime.defaults', return_value={
                    'forgejo_image': store.references[2],
                    'forgejo_postgres_image': store.references[3]}), \
                    patch.object(RunnerRun, 'command', side_effect=store.command):
                assets, configuration = module.prepare(directory, descriptor, 'amd64')
            self.assertEqual([row['id'] for row in configuration['images']],
                             ['1' * 64, '2' * 64, '3' * 64, '4' * 64])
            self.assertEqual(len(assets), 6)
            for index in range(4):
                self.assertEqual((directory / f'image-{index}.oci').read_bytes(),
                                 b'public OCI asset')

    def test_verified_cached_assets_work_after_registry_manifest_retirement(self):
        try:
            self.prepare_assets(ImageStore())
        except RuntimeError as error:
            self.fail(f'Cached pinned images must not require the retired registry manifest: {error}')

    def test_missing_assets_are_downloaded_from_the_pinned_registry(self):
        self.prepare_assets(ImageStore(cached=False, registry=True))

    def test_cache_engine_failure_is_not_treated_as_a_missing_image(self):
        with self.assertRaises(RuntimeError):
            self.prepare_assets(ImageStore(registry=True, exists_error=True))

    def test_cached_assets_with_wrong_identity_or_architecture_are_rejected(self):
        for change in ({'Architecture': 'arm64'}, {'RepoDigests': []}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare_assets(ImageStore(registry=True, image_change=change))

    def test_wrong_architecture_or_digest_is_rejected(self):
        module = importlib.import_module('scripts.forgejo_runner.native_assets')
        pin = 'example.invalid/image:version@sha256:' + 'a' * 64
        image = {'Id': 'b' * 64, 'Architecture': 'amd64',
                 'RepoDigests': ['example.invalid/image@sha256:' + 'a' * 64]}
        self.assertEqual('b' * 64, module.validate_image(image, pin, 'amd64'))
        for change in ({'Architecture': 'arm64'}, {'RepoDigests': []}, {'Id': 'unknown'},
                       {'RepoDigests': ['example.invalid/image@sha256:' + 'c' * 64]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.validate_image({**image, **change}, pin, 'amd64')


if __name__ == '__main__':
    unittest.main()
