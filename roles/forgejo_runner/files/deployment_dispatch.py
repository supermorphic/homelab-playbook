"""Observe or maintain the existing revision before replacing its definitions."""

import json
import os
from pathlib import Path
import re
import stat
import sys


def arguments(mode, value, configuration, repository=None):
    slot = value.get('slot', {})
    name = slot.get('name', '')
    installation = value.get('installation', '')
    if (mode not in ('verify', 'drain', 'recover', 'restore-authority')
            or re.fullmatch(r'[a-z][a-z0-9-]{0,47}', name) is None
            or slot.get('state_root') != '/var/lib/forgejo-runner/' + name
            or str(configuration) != slot['state_root'] + '/config.json'
            or re.fullmatch(r'/usr/local/libexec/forgejo-runner/[0-9a-f]{40}', installation) is None):
        raise ValueError('Installed operation is outside its canonical boundary')
    if mode == 'restore-authority' and repository not in slot.get('repositories', []):
        raise ValueError('Replacement authority does not match the installed repository')
    entry = 'control.py' if mode in ('verify', 'restore-authority') else 'maintenance.py'
    argv = ['/usr/bin/python3', '-B', installation + '/' + entry, mode, '--config', str(configuration)]
    return argv + ['--repository', repository] if mode == 'restore-authority' else argv


def main():
    try:
        if len(sys.argv) not in (3, 4) or os.geteuid() != 0:
            raise ValueError('Installed operation requires an explicit administrator mode and configuration')
        path = Path(sys.argv[2])
        for parent in (path.parent, *path.parent.parents):
            metadata = parent.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise ValueError('Configuration parent is replaceable')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0
                    or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise ValueError('Installed configuration has unsafe metadata')
            raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError('Installed configuration exceeds its bound')
        value = json.loads(raw)
        if len(sys.argv) == 4 and sys.argv[1] == 'verify':
            if sys.argv[3] != '--expected-slot-stdin':
                raise ValueError('Unsupported verification input')
            expected = sys.stdin.read(65537)
            if len(expected) > 65536 or json.loads(expected) != value['slot']:
                raise ValueError('Installed slot differs from its declared repository configuration')
        argv = arguments(sys.argv[1], value, path, sys.argv[3] if len(sys.argv) == 4 else None)
        entry = Path(argv[2])
        for parent in (entry.parent, *entry.parent.parents):
            metadata = parent.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise ValueError('Installed helper parent is replaceable')
        metadata = entry.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022):
            raise ValueError('Installed helper has unsafe metadata')
        os.execve(argv[0], argv, {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    except Exception as error:
        print(json.dumps({'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
