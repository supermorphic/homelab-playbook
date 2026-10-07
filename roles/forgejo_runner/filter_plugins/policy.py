"""Validate explicit repository slots before provisioning any host resources."""

import importlib.util
from pathlib import Path
import re
import stat
import hashlib
import sys
import subprocess


SLOT_FIELDS = {'name', 'repository', 'enabled', 'label', 'state_root',
               'controller', 'worker', 'limits'}
LIMIT_FIELDS = {'memory_bytes', 'disk_bytes', 'pids', 'cpu_percent',
               'idle_seconds', 'job_seconds'}
LIMIT_MAXIMUMS = {'memory_bytes': 2 * 1024**3, 'disk_bytes': 24 * 1024**3,
                  'pids': 768, 'cpu_percent': 200, 'idle_seconds': 300, 'job_seconds': 10800}


def validate_slots(slots):
    if not isinstance(slots, list):
        raise ValueError('Runner slots must be a list of explicit declarations')
    accounts = []
    for slot in slots:
        if not isinstance(slot, dict) or set(slot) != SLOT_FIELDS:
            raise ValueError('Runner slot has missing or unexpected fields')
        for key in ('name', 'label'):
            if (not isinstance(slot[key], str)
                    or re.fullmatch(r'[a-z][a-z0-9-]{0,47}', slot[key]) is None):
                raise ValueError('Runner slot name and label must be bounded identifiers')
        if (not isinstance(slot['repository'], str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',
                                slot['repository']) is None):
            raise ValueError('Runner registration requires an exact owner/repository')
        if type(slot['enabled']) is not bool:
            raise ValueError('Runner enablement must be an explicit boolean')
        if slot['state_root'] != '/var/lib/forgejo-runner/' + slot['name']:
            raise ValueError('Runner state must belong to its named slot')
        limits = slot['limits']
        if (not isinstance(limits, dict) or set(limits) != LIMIT_FIELDS
                or any(not isinstance(value, int) or isinstance(value, bool)
                       or not 0 < value <= LIMIT_MAXIMUMS[key]
                       for key, value in limits.items())):
            raise ValueError('Every runner resource limit must be a bounded positive integer')
        for key in ('controller', 'worker'):
            account = slot[key]
            if (not isinstance(account, dict)
                    or account.get('subuid_count') != account.get('subgid_count')):
                raise ValueError('Runner identity requires matching UID and GID mapping counts')
            accounts.append(account)
    for key in ('name', 'state_root'):
        values = [slot[key] for slot in slots]
        if len(values) != len(set(values)):
            raise ValueError('Duplicate runner slot ownership')
    repositories = [slot['repository'].casefold() for slot in slots]
    if len(repositories) != len(set(repositories)):
        raise ValueError('A repository can have only one declared runner slot')
    # Reuse the foundation's declaration and cross-account allocation rules.
    # The owning role separately compares these declarations with live host
    # databases before provision; declaration validation never claims that check.
    path = Path(__file__).resolve().parents[2] / 'podman_foundation/filter_plugins/identity.py'
    spec = importlib.util.spec_from_file_location('runner_foundation_identity', path)
    foundation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(foundation)
    foundation.validate_accounts(accounts, '', '', '', '')
    if any(account['uid'] < 1000 or account['gid'] < 1000 for account in accounts):
        raise ValueError('Runner identity must match the qualified runtime UID and GID floor')
    return slots


def validate_unused_slots(slots, passwd, groups, subuids, subgids):
    validate_slots(slots)
    accounts = [slot[key] for slot in slots for key in ('worker', 'controller')]
    path = Path(__file__).resolve().parents[2] / 'podman_foundation/filter_plugins/identity.py'
    spec = importlib.util.spec_from_file_location('runner_foundation_identity', path)
    foundation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(foundation)
    foundation.validate_accounts(accounts, passwd, groups, subuids, subgids)
    names = {row.split(':', 1)[0] for source in (passwd, groups) for row in source.splitlines() if row}
    if any(account['name'] in names for account in accounts):
        raise ValueError('Disposable runner identities cannot adopt existing host accounts')
    return slots


def validate_public_assets(manifest, directory):
    root = Path(__file__).resolve().parents[3]
    anchor = root / '.tmp'
    source = Path(directory)
    if not source.is_absolute():
        source = root / source
    if not source.is_relative_to(anchor) or '..' in source.parts:
        raise ValueError('Deployment artifacts must belong to this worktree .tmp')
    for parent in (source, *source.parents):
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError('Deployment artifact directory contains a linked boundary')
        if parent == anchor:
            break
    if (not isinstance(manifest, dict) or not isinstance(manifest.get('artifacts'), dict)
            or set(manifest['artifacts']) != {'job.oci', 'forgejo-runner', 'netavark'}):
        raise ValueError('Deployment artifact manifest is incomplete')
    spec = importlib.util.spec_from_file_location('runner_public_assets',
        root / 'scripts/forgejo_runner/deployment_assets.py')
    assets = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(assets)
    try:
        for name, expected in manifest['artifacts'].items():
            assets.verify(source / name, expected)
    except (OSError, ValueError):
        raise ValueError('Deployment artifact did not match its public manifest') from None
    return manifest


def validate_manifest(manifest, candidate):
    root = str(Path(__file__).resolve().parents[3])
    sys.path.insert(0, root)
    try:
        from scripts.forgejo_runner.workload import registry_sources, SCENARIOS
    finally:
        sys.path.remove(root)
    expected = {'schema', 'architecture', 'source_tree', 'job_image_id', 'sources', 'registry_images', 'artifacts'}
    if (not isinstance(manifest, dict) or set(manifest) != expected
            or manifest['schema'] != 1 or isinstance(manifest['schema'], bool)
            or manifest['architecture'] != 'amd64'
            or not isinstance(manifest['source_tree'], str)
            or re.fullmatch(r'[0-9a-f]{40}', manifest['source_tree']) is None
            or not isinstance(manifest['job_image_id'], str)
            or re.fullmatch(r'[0-9a-f]{64}', manifest['job_image_id']) is None
            or manifest['sources'] != {
                'probe_image': candidate['probe_images']['amd64'],
                'runner_image': candidate['runner_images']['amd64'],
                'mise_image': candidate['mise_images']['amd64']}):
        raise ValueError('Deployment manifest does not match the reviewed native sources')
    images = manifest['registry_images']
    allowed = registry_sources(suite='stock-runtime')
    for selector in SCENARIOS:
        allowed |= registry_sources(selector)
    if (not isinstance(images, list) or not 1 <= len(images) <= 64
            or any(not isinstance(reference, str) or reference not in allowed for reference in images)
            or len(set(images)) != len(images)):
        raise ValueError('Deployment registry images are outside the qualified workload set')
    artifacts = manifest['artifacts']
    if (not isinstance(artifacts, dict) or set(artifacts) != {'job.oci', 'forgejo-runner', 'netavark'}
            or any(not isinstance(value, dict) or set(value) != {'size', 'sha256'}
                   or not isinstance(value['size'], int) or isinstance(value['size'], bool)
                   or not 0 < value['size'] <= 2 * 1024**3 or not isinstance(value['sha256'], str)
                   or re.fullmatch(r'[0-9a-f]{64}', value['sha256']) is None
                   for value in artifacts.values())):
        raise ValueError('Deployment artifacts require bounded checksums')
    return manifest


def validate_provenance(manifest, source_commit, *, root=None):
    root = root or Path(__file__).resolve().parents[3]
    def git(*arguments):
        return subprocess.check_output(['git', '-C', str(root), *arguments],
            text=True, stderr=subprocess.PIPE, timeout=15).strip()
    try:
        commit = git('rev-parse', 'HEAD')
        tree = git('rev-parse', 'HEAD^{tree}')
        staged = git('write-tree')
        changed = git('diff', '--name-only')
    except (subprocess.SubprocessError, OSError):
        raise ValueError('Deployment source provenance cannot be verified') from None
    if (source_commit != commit or manifest.get('source_tree') != tree
            or staged != tree or changed):
        raise ValueError('Provision only artifacts bound to the exact clean committed candidate')
    return manifest


def deployment_sources(helper_root):
    if re.fullmatch(r'/usr/local/libexec/forgejo-runner/[0-9a-f]{40}', helper_root) is None:
        raise ValueError('Helper installation must use its immutable source commit')
    root = Path(__file__).resolve().parents[3]
    role_files = Path(__file__).resolve().parent.parent / 'files'
    sources = []
    for path in sorted(role_files.glob('*.py')):
        sources.append((path, helper_root + '/' + path.name))
    for path in sorted((root / 'scripts/forgejo_runner').glob('*.py')):
        sources.append((path, helper_root + '/scripts/forgejo_runner/' + path.name))
        if path.stem in ('worker_launch', 'worker_probe', 'native_tools', 'host_network',
                         'host_egress', 'network_setup', 'gateway_launch'):
            sources.append((path, helper_root + '/' + path.name))
    output = []
    for path, destination in sources:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 1024**2:
            raise ValueError('Helper source is not a bounded public regular file')
        output.append({'src': str(path), 'path': destination, 'mode': 0o644,
                       'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return output


class FilterModule:
    def filters(self):
        return {'forgejo_runner_validate_slots': validate_slots,
                'forgejo_runner_validate_unused_slots': validate_unused_slots,
                'forgejo_runner_validate_public_assets': validate_public_assets,
            'forgejo_runner_validate_manifest': validate_manifest,
            'forgejo_runner_validate_provenance': validate_provenance,
                'forgejo_runner_deployment_sources': deployment_sources}
