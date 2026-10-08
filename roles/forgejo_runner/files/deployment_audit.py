"""Read-only file comparison and admission checks before role reconciliation."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


def file_digest(path, *, authority_uid=0, maximum=2 * 1024**3):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != authority_uid or before.st_size > maximum):
            raise ValueError('Existing deployment file has unsafe ownership or type')
        digest = hashlib.sha256()
        while chunk := stream.read(1024**2):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('Deployment input changed during observation')
    return digest.hexdigest(), stat.S_IMODE(before.st_mode)


def audit_files(files, *, idle, authority_uid=0, plan=False):
    changed = False
    for row in files:
        try:
            actual = file_digest(row['path'], authority_uid=authority_uid)
        except FileNotFoundError:
            changed = True
        else:
            changed |= actual != (row['sha256'], row['mode'])
    if changed and not idle and not plan:
        raise ValueError('Drain all shared worker slots before changing deployment inputs')
    return {'changed': changed, 'idle': idle}


def host_idle(slots):
    ids, ranges = set(), []
    clean = True
    for slot in slots:
        root = Path(slot['state_root'])
        for parent in (root, *root.parents):
            try:
                metadata = parent.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
                    or metadata.st_mode & 0o022):
                raise ValueError('Runner state has a replaceable parent boundary')
        state = root / 'state.json'
        try:
            file_digest(state, maximum=16384)
        except FileNotFoundError:
            pass
        else:
            if stat.S_IMODE(state.lstat().st_mode) != 0o600:
                raise ValueError('Runner checkpoint is not private')
            descriptor = os.open(state, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor) as stream:
                value = json.load(stream)
            clean &= value.get('phase') == 'clean' and value.get('generation') is None
        clean &= not any(os.path.lexists(root / path)
                         for path in ('runtime', 'resources.json', 'worker.ext4'))
        for key in ('worker', 'controller'):
            account = slot[key]
            ids.add(account['uid'])
            ranges.append((account['subuid_start'], account['subuid_count']))
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            source = (process / 'status').read_text()
        except FileNotFoundError:
            continue
        for line in source.splitlines():
            if line.startswith('Uid:'):
                uid = int(line.split()[2])
                if uid in ids or any(start <= uid < start + count for start, count in ranges):
                    clean = False
                break
    result = subprocess.run(['/usr/bin/systemctl', 'list-units', '--all', '--plain', '--output=json',
                             'forgejo-worker-*'], capture_output=True, text=True, timeout=15, check=True,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    if len(result.stdout) > 65536:
        raise ValueError('Worker unit observation exceeded its bound')
    units = json.loads(result.stdout)
    return bool(clean and not units)


def main():
    try:
        raw = sys.stdin.read(1024**2 + 1)
        if len(raw) > 1024**2:
            raise ValueError('Deployment comparison exceeds its bound')
        request = json.loads(raw)
        result = audit_files(request['files'], idle=host_idle(request['slots']), plan=request.get('plan') is True)
    except Exception as error:
        print(json.dumps({'error_type': type(error).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
