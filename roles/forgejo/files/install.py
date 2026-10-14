"""Publish immutable compatible helper bundles after checking installed pins."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
sys.dont_write_bytecode = True
import tempfile

import service
import upgrade_state

FILES = ('profiles.py', 'service.py', 'archive.py', 'backup.py', 'transfer.py',
         'mirror_status.py', 'upgrade_state.py', 'install.py', 'upgrade.py',
         'admission.py', 'upgrade_host.py')


def launcher(owner):
    return ('''"""Load one immutable, administrator-owned helper generation."""
import hashlib, json, os, pathlib, re, stat, sys
sys.dont_write_bytecode = True
root = pathlib.Path(__file__).resolve().parent
record = root / 'helper-generation.json'
metadata = record.lstat()
if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != OWNER or metadata.st_mode & 0o022:
    raise RuntimeError('unsafe helper generation selector')
selected = json.loads(record.read_text())
generation = selected['generation']
if re.fullmatch(r'[0-9a-f]{64}', generation) is None:
    raise RuntimeError('invalid helper generation')
bundle = root / 'bundles' / generation
for directory in (root, root / 'bundles', bundle):
    item = directory.lstat()
    if not stat.S_ISDIR(item.st_mode) or item.st_uid != OWNER or item.st_mode & 0o022:
        raise RuntimeError('unsafe helper generation directory')
source = bundle / pathlib.Path(__file__).name
item = source.lstat()
if not stat.S_ISREG(item.st_mode) or item.st_uid != OWNER or item.st_mode & 0o022 or item.st_nlink != 1:
    raise RuntimeError('unsafe helper generation file')
content = source.read_bytes()
if hashlib.sha256(content).hexdigest() != selected['hashes'][source.name]:
    raise RuntimeError('helper generation checksum mismatch')
sys.path.insert(0, str(bundle))
__file__ = str(source)
exec(compile(content, str(source), 'exec'), globals())
'''.replace('OWNER', str(owner))).encode()


def publish_bundle(staged, destination, expected, installed, *, owner=0, group=0):
    if installed is not None and any(installed.get(key) != expected[key]
                                    for key in ('image', 'postgres_image')):
        raise ValueError('runtime upgrade must not replace installed helpers through provision')
    upgrade_state.require_idle(destination, owner=owner)
    service.check_parent_chain(staged)
    metadata = staged.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != owner or metadata.st_mode & 0o077:
        raise ValueError('helper candidate must be a private administrator-owned directory')
    if {path.name for path in staged.iterdir()} != set(FILES):
        raise ValueError('helper candidate does not match the fixed bundle contract')
    content = {name: service.safe_read(staged / name, owner=owner) for name in FILES}
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in content.items()}
    selector = {'generation': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
                'hashes': hashes}
    bundles = destination / 'bundles'
    service.check_parent_chain(bundles)
    bundles.mkdir(mode=0o750, exist_ok=True)
    os.chown(bundles, owner, group)
    bundle = bundles / selector['generation']
    if bundle.exists() or bundle.is_symlink():
        service.check_parent_chain(bundle)
        if {path.name for path in bundle.iterdir()} != set(FILES):
            raise ValueError('existing helper bundle differs from its immutable identity')
        for name, value in content.items():
            if service.safe_read(bundle / name, owner=owner) != value:
                raise ValueError('existing helper bundle differs from its immutable identity')
    else:
        scratch = Path(tempfile.mkdtemp(prefix='.partial-', dir=bundles))
        try:
            os.chown(scratch, owner, group)
            for name, value in content.items():
                service.atomic_write(scratch / name, value, mode=0o640, uid=owner, gid=group)
            scratch.chmod(0o750)
            scratch.rename(bundle)
            descriptor = service.open_directory(bundles)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if scratch.exists():
                shutil.rmtree(scratch)
    changed = False
    entry = launcher(owner)
    for name in FILES:
        path = destination / name
        if path.exists() and service.safe_read(path, owner=owner) == entry:
            continue
        service.atomic_write(path, entry, mode=0o640, uid=owner, gid=group)
        changed = True
    path = destination / 'helper-generation.json'
    data = (json.dumps(selector, sort_keys=True) + '\n').encode()
    if not path.exists() or service.safe_read(path, owner=owner) != data:
        service.atomic_write(path, data, mode=0o640, uid=owner, gid=group)
        changed = True
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description='Install compatible Forgejo lifecycle helpers')
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--postgres-image', required=True)
    options = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise ValueError('helper installation requires the managed administrator')
        allocation = service.account_allocation()
        service.check_boundaries(allocation)
        with service.operation_lock(service.LOCK, 300), service.transfer_operation_lock(allocation):
            stored = service.HELPERS / 'desired.json'
            installed = json.loads(service.safe_read(stored, owner=0)) if stored.exists() else None
            expected = {'image': options.image, 'postgres_image': options.postgres_image}
            if installed is not None and any(installed.get(key) != expected[key] for key in expected):
                raise ValueError('runtime upgrade must not replace installed helpers through provision')
            upgrade_state.require_idle(service.HELPERS)
            timers = ('forgejo-backup.timer', 'forgejo-transfer.timer')
            active = [unit for unit in timers if service.manager(['is-active', unit], check=False).returncode == 0]
            try:
                for unit in active:
                    service.manager(['stop', unit])
                for unit in ('forgejo-backup.service', 'forgejo-recover.service', 'forgejo-transfer.service'):
                    state = service.manager(['show', '--property=ActiveState', '--value', unit], check=False)
                    if state.returncode == 0 and state.stdout.strip() not in ('', 'inactive', 'failed'):
                        raise RuntimeError('active helper operation must complete before helper installation')
                result = publish_bundle(options.candidate, service.HELPERS, expected, installed,
                                        group=allocation['gid'])
            finally:
                for unit in active:
                    service.manager(['start', unit])
        print(json.dumps({'changed': result}))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        print('Forgejo helper installation failed; check source pins and pending operations', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
