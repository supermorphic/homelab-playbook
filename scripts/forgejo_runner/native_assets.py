"""Prepare public pinned OCI images and a staged source snapshot for an offline job."""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile


def validate_image(image, pin, architecture):
    repository, digest = pin.split('@')
    prefix, leaf = repository.rsplit('/', 1)
    canonical = prefix + '/' + leaf.split(':', 1)[0] + '@' + digest
    identity = image.get('Id', '').removeprefix('sha256:')
    if (image.get('Architecture') != architecture or canonical not in image.get('RepoDigests', [])
            or re.fullmatch(r'[0-9a-f]{64}', identity) is None):
        raise ValueError('Pinned native image identity or architecture does not match')
    return identity


def asset_descriptor(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return {'size': path.stat().st_size, 'sha256': digest.hexdigest()}


def prepare(directory, descriptor, architecture, *, workload=None):
    from scripts.forgejo.runtime import defaults
    from scripts.forgejo_runner.fixture import ROOT, load_candidate, RunnerRun
    from scripts.forgejo_runner.host_inputs import validate_assets
    from scripts.forgejo_runner.native_tools import prepare_archive
    from scripts.forgejo_runner.workload import source_tree
    if architecture != 'amd64':
        raise ValueError('Native job experiment currently requires amd64')
    candidate = load_candidate(descriptor, architecture)
    if not candidate['runner_image']:
        raise ValueError('Native job requires an immutable runner image')
    sources = [candidate['probe_image'], candidate['runner_image']]
    selected_mise = None
    if workload is not None:
        from scripts.forgejo_runner.workload import validation_script, mise_image
        validation_script(**workload)
        selected_mise = mise_image(descriptor, architecture)
    pins = defaults()
    sources += [pins['forgejo_image'], pins['forgejo_postgres_image']]
    if selected_mise:
        sources.append(selected_mise)
    experiment = RunnerRun()
    auth = experiment.private_file(directory, 'registry-auth.json', '{"auths":{}}\n')
    assets = {}
    rows = []
    for index, pin in enumerate(sources):
        cached = experiment.command([experiment.podman, 'image', 'exists', pin], check=False)
        if cached.returncode == 1:
            experiment.command([experiment.podman, 'pull', '--authfile', str(auth),
                                '--arch', architecture, pin], timeout=600)
        elif cached.returncode != 0:
            raise RuntimeError('Native image cache cannot be inspected')
        image = json.loads(experiment.command([experiment.podman, 'image', 'inspect', pin]).stdout)[0]
        identity = validate_image(image, pin, architecture)
        name = f'image-{index}.oci'
        path = directory / name
        experiment.command([experiment.podman, 'save', '--format=oci-archive',
                            '--output', str(path), identity], timeout=300)
        path.chmod(0o600)
        assets['work/input/' + name] = asset_descriptor(path)
        validate_assets(assets, 2 * 1024**3)
        rows.append({'source': pin, 'id': identity, 'asset': name})
    tree = source_tree(ROOT)
    path = directory / 'source.tar'
    subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', '--output', str(path), tree],
                   capture_output=True, check=True, timeout=60)
    path.chmod(0o600)
    assets['work/input/source.tar'] = asset_descriptor(path)
    # Only these already-pinned Python packages are needed by the trusted
    # synthetic coordinator. Copy their portable Python implementation, never
    # operator configuration, compiled host extensions or the whole environment.
    path = directory / 'vendor.tar'
    import yaml
    import jinja2
    import markupsafe
    with tarfile.open(path, 'w') as archive:
        for package in (yaml, jinja2, markupsafe):
            root = Path(package.__file__).parent
            for source in sorted(root.rglob('*.py')):
                if source.is_symlink():
                    raise ValueError('Unexpected symlink in pinned Python package')
                archive.add(source, arcname=package.__name__ + '/' + str(source.relative_to(root)), recursive=False)
    path.chmod(0o600)
    assets['work/input/vendor.tar'] = asset_descriptor(path)
    path = prepare_archive(directory, architecture)
    assets['work/input/netavark.tar'] = asset_descriptor(path)
    validate_assets(assets, 2 * 1024**3)
    return assets, {'architecture': architecture, 'source_tree': tree, 'images': rows,
                    'candidate': candidate, **({'workload':workload, 'mise_image':selected_mise} if workload else {})}
