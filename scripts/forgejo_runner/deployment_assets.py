"""Build immutable public deployment artifacts; no enrollment credentials."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]


def describe(path, *, maximum=2 * 1024**3):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or not 0 < metadata.st_size <= maximum):
            raise ValueError('Artifact must be a bounded unlinked regular file')
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(min(1024**2, maximum + 1 - size)):
            size += len(chunk)
            digest.update(chunk)
            if size > maximum:
                raise ValueError('Artifact exceeds its fixed bound')
        after = os.fstat(stream.fileno())
        if (metadata.st_size != size or metadata.st_mtime_ns != after.st_mtime_ns
                or metadata.st_ctime_ns != after.st_ctime_ns):
            raise ValueError('Artifact changed during measurement')
    return {'size': size, 'sha256': digest.hexdigest()}


def verify(path, expected):
    if not isinstance(expected, dict) or set(expected) != {'size', 'sha256'} or describe(path) != expected:
        raise ValueError('Artifact does not match its approved manifest')


def validate_executable(path):
    describe(path, maximum=128 * 1024**2)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        header = stream.read(64)
    if (len(header) != 64 or header[:7] != b'\x7fELF\x02\x01\x01'
            or header[16:18] not in (b'\x02\x00', b'\x03\x00') or header[18:20] != b'\x3e\x00'):
        raise ValueError('Controller executable does not use the accepted amd64 ELF ABI')


def ensure_image(run, pin, auth):
    from scripts.forgejo_runner.native_assets import validate_image
    cached = run.command([run.podman, 'image', 'exists', pin], check=False)
    if cached.returncode == 1:
        run.command([run.podman, 'pull', '--authfile', str(auth), '--arch=amd64', pin], timeout=600)
    elif cached.returncode != 0:
        raise RuntimeError('Deployment image cache cannot be inspected')
    image = json.loads(run.command([run.podman, 'image', 'inspect', pin]).stdout)[0]
    return validate_image(image, pin, 'amd64')


def prepare(descriptor, destination):
    from scripts.forgejo_runner.fixture import RunnerRun, load_candidate
    from scripts.forgejo_runner.workload import build_image, mise_image, source_tree, registry_sources, SCENARIOS
    from scripts.forgejo_runner.native_tools import prepare_archive, install_archive
    candidate = load_candidate(descriptor, 'amd64')
    if candidate['runner_image'] is None:
        raise ValueError('Deployment needs an immutable runner image')
    selected_mise = mise_image(descriptor, 'amd64')
    destination.mkdir(mode=0o700)
    run = RunnerRun()
    primary = None
    try:
        run.observe_volumes()
        auth = run.private_file(destination, 'registry-auth.json', '{"auths":{}}\n')
        for pin in (candidate['runner_image'], candidate['probe_image'], selected_mise):
            ensure_image(run, pin, auth)
        job = build_image(run, descriptor, 'amd64')
        image = json.loads(run.command([run.podman, 'image', 'inspect', job]).stdout)[0]
        if image.get('Architecture') != 'amd64':
            raise ValueError('Built deployment image has the wrong architecture')
        job_id = image['Id'].removeprefix('sha256:')
        run.command([run.podman, 'save', '--format=oci-archive', '--output', str(destination / 'job.oci'), job_id], timeout=300)
        container = run.create_controller('asset-controller', ['--pull=never', '--network=none',
            '--entrypoint=/bin/sleep', candidate['runner_image'], '300'])
        run.command([run.podman, 'cp', container + ':/bin/forgejo-runner', str(destination / 'forgejo-runner')])
        validate_executable(destination / 'forgejo-runner')
        network_archive = prepare_archive(destination, 'amd64')
        install_archive(network_archive, destination / 'netavark')
        network_archive.unlink()
        artifacts = {}
        for name in ('job.oci', 'forgejo-runner', 'netavark'):
            path = destination / name
            path.chmod(0o600)
            artifacts[name] = describe(path)
        registry_images = registry_sources(suite='stock-runtime')
        for selector in SCENARIOS:
            registry_images |= registry_sources(selector)
        manifest = {'schema': 1, 'architecture': 'amd64', 'source_tree': source_tree(ROOT),
                    'job_image_id': job_id, 'sources': {**candidate, 'mise_image': selected_mise},
                    'registry_images': sorted(registry_images), 'artifacts': artifacts}
        path = destination / 'manifest.json'
        with path.open('x') as stream:
            json.dump(manifest, stream, sort_keys=True, indent=2)
            stream.write('\n')
        path.chmod(0o600)
        auth.unlink()
    except BaseException as error:
        primary = error
    errors = run.cleanup()
    if errors:
        raise RuntimeError('Deployment artifact builder cleanup requires attention') from None
    if primary is not None:
        raise RuntimeError('Deployment artifact preparation failed') from None
    return manifest


def main():
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', required=True, type=Path, help='Approved immutable image descriptor')
    parser.add_argument('--output', required=True, type=Path, help='New private directory under this worktree .tmp')
    args = parser.parse_args()
    destination = args.output.resolve()
    if not destination.is_relative_to((ROOT / '.tmp').resolve()) or destination.exists():
        parser.error('Artifact output must be a new private directory under this worktree .tmp')
    prepare(args.fixture, destination)
    print('Public deployment artifacts and their manifest are ready')


if __name__ == '__main__':
    main()
