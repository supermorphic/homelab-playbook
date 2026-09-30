"""Shared exact-generation Forgejo archive contract; no runtime mutations."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile

PAYLOADS = ('database.dump', 'files.tar.gz', 'manifest.json', 'SHA256SUMS')
LAYOUT = {'data': '/var/lib/gitea', 'config': '/etc/gitea'}
NAME = re.compile(r'forgejo-(\d{8}T\d{6}Z)-([a-z0-9]{8,32})')


def archive_time(name: str):
    matched = NAME.fullmatch(name)
    if not matched:
        raise ValueError('invalid archive identifier')
    return datetime.strptime(matched[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)


def read_file(path: Path) -> bytes:
    # Callers supply private, run-owned or service-owned directories.
    item = path.lstat()
    if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1
            or item.st_uid != os.geteuid() or item.st_mode & 0o077):
        raise ValueError('archive payload has unsafe metadata')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        current = os.fstat(stream.fileno())
        if (current.st_dev, current.st_ino) != (item.st_dev, item.st_ino):
            raise ValueError('archive payload changed during validation')
        return stream.read()


def digest_file(path: Path) -> dict:
    # Stream large payloads; descriptor pins the checked inode.
    item = path.lstat()
    if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1
            or item.st_uid != os.geteuid() or item.st_mode & 0o077):
        raise ValueError('archive payload has unsafe metadata')
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        current = os.fstat(stream.fileno())
        if (current.st_dev, current.st_ino) != (item.st_dev, item.st_ino):
            raise ValueError('archive payload changed during validation')
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
            size += len(block)
        after = os.fstat(stream.fileno())
        if after.st_size != size or after.st_mtime_ns != current.st_mtime_ns:
            raise ValueError('archive payload changed during validation')
    return {'sha256': digest.hexdigest(), 'size': size}


def validate_tar(path: Path):
    names = set()
    with tarfile.open(path, 'r:gz') as stream:
        for item in stream:
            name = PurePosixPath(item.name)
            if (name.is_absolute() or '..' in name.parts or not name.parts
                    or name.parts[0] not in LAYOUT or item.name != name.as_posix()
                    or item.name in names
                    or not (item.isfile() or item.isdir())
                    or item.mode & 0o7000 or item.uid != 1000 or item.gid != 1000):
                raise ValueError('file archive contains unsupported path, type or ownership')
            names.add(item.name)
    if not names:
        raise ValueError('empty file archive')


def validate_manifest(manifest: dict):
    created = archive_time(manifest['archive_id'])
    if (manifest.get('schema') != 1 or manifest.get('postgres_major') != 17
            or manifest.get('layout') != LAYOUT
            or manifest.get('created_at') != created.strftime('%Y-%m-%dT%H:%M:%SZ')
            or re.fullmatch(r'[0-9a-f]{32}', manifest.get('recovery_generation', '')) is None):
        raise ValueError('incompatible archive manifest')
    patterns = {
        'image': r'codeberg[.]org/forgejo/forgejo:15[.]\d+[.]\d+-rootless@sha256:[0-9a-f]{64}',
        'postgres_image': r'docker[.]io/library/postgres:17[.]\d+@sha256:[0-9a-f]{64}',
        'rclone_image': r'docker[.]io/rclone/rclone:\d+[.]\d+[.]\d+@sha256:[0-9a-f]{64}',
    }
    for key, pattern in patterns.items():
        if re.fullmatch(pattern, manifest.get(key, '')) is None:
            raise ValueError('incompatible archive image pin')
    return manifest


def completion(path: Path) -> dict:
    manifest = validate_manifest(json.loads(read_file(path / 'manifest.json')))
    return {'schema': 1, 'archive_id': manifest['archive_id'],
            'files': {name: digest_file(path / name) for name in PAYLOADS}}


def validate_archive(path: Path, require_complete: bool) -> dict:
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077):
        raise ValueError('archive directory must be private and owned')
    expected = set(PAYLOADS)
    if (path / 'COMPLETE').exists() or require_complete:
        expected.add('COMPLETE')
    if {item.name for item in path.iterdir()} != expected:
        raise ValueError('archive does not contain the complete fixed payload set')
    manifest = validate_manifest(json.loads(read_file(path / 'manifest.json')))
    if path.name not in (manifest['archive_id'], '.partial-' + manifest['archive_id']):
        raise ValueError('archive directory differs from manifest identifier')
    actual = completion(path)
    sums = ''.join(actual['files'][name]['sha256'] + '  ' + name + '\n'
                   for name in PAYLOADS[:3])
    if read_file(path / 'SHA256SUMS').decode() != sums:
        raise ValueError('archive payload checksum mismatch')
    with (path / 'database.dump').open('rb') as stream:
        if stream.read(5) != b'PGDMP':
            raise ValueError('database archive is not PostgreSQL custom format')
    validate_tar(path / 'files.tar.gz')
    if 'COMPLETE' in expected and json.loads(read_file(path / 'COMPLETE')) != actual:
        raise ValueError('completion metadata differs from the verified payload set')
    return manifest


def seal(path: Path, manifest: dict):
    validate_manifest(manifest)
    target = path / 'manifest.json'
    with target.open('x') as stream:
        stream.write(json.dumps(manifest, sort_keys=True) + '\n')
    target.chmod(0o600)
    sums = ''.join(digest_file(path / name)['sha256'] + '  ' + name + '\n'
                   for name in PAYLOADS[:3])
    target = path / 'SHA256SUMS'
    with target.open('x') as stream:
        stream.write(sums)
    target.chmod(0o600)
    validate_archive(path, False)
    for name in PAYLOADS:
        descriptor = os.open(path / name, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
