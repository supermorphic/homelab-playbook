"""Observational runner verification: no repair, enrollment or runtime client."""

from decimal import Decimal
import os
from pathlib import Path
import re
import subprocess

from lifecycle import read_checkpoint


PROPERTIES = ('LoadState,ActiveState,InvocationID,RootImage,MemoryMax,MemorySwapMax,'
              'TasksMax,CPUQuotaPerSecUSec,KillMode')


def execute(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}, check=False)
    if len(result.stdout) > 16384 or result.returncode and 'LoadState=not-found' not in result.stdout:
        raise RuntimeError('Runner observation did not return bounded evidence')
    return result.stdout


def process_rows():
    rows = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            source = (entry / 'status').read_text()
        except FileNotFoundError:
            continue
        for line in source.splitlines():
            if line.startswith('Uid:'):
                rows.append({'uid': int(line.split()[2])})
                break
    return rows


def quota_matches(value, percent):
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(us|ms|s)', value)
    return bool(match and Decimal(match[1]) * {'us': 1, 'ms': 1000, 's': 1000000}[match[2]]
                == percent * 10000)


def observe(config, *, execute=execute, authority_uid=0, process_reader=process_rows):
    state = read_checkpoint(config['state_root'], authority_uid=authority_uid)
    root = Path(config['state_root'])
    generation = state['generation']
    unit = 'forgejo-worker-' + config['name'] + ('-' + generation if generation else '') + '.service'
    document = execute(['systemctl', 'show', '--property=' + PROPERTIES, '--', unit])
    runtime = dict(line.split('=', 1) for line in document.splitlines() if '=' in line)
    owned = 0
    for row in process_reader():
        for key in ('worker', 'controller'):
            account = config.get(key)
            if account and (row['uid'] == account['uid']
                            or account['subuid_start'] <= row['uid']
                            < account['subuid_start'] + account['subuid_count']):
                owned += 1
                break
    absent = (runtime.get('LoadState') == 'not-found' and owned == 0
              and not os.path.lexists(root / 'runtime') and not os.path.lexists(root / 'worker.ext4'))
    limits = config['limits']
    allocation = state['allocation']
    matched = (allocation is not None and runtime.get('LoadState') == 'loaded'
               and runtime.get('ActiveState') == 'active'
               and runtime.get('InvocationID') == allocation['invocation']
               and runtime.get('RootImage') == str(root / 'runtime' / 'worker.ext4'))
    bounded = bool(matched and runtime.get('MemoryMax') == str(limits['memory_bytes'])
                   and runtime.get('MemorySwapMax') == '0' and runtime.get('TasksMax') == str(limits['pids'])
                   and quota_matches(runtime.get('CPUQuotaPerSecUSec'), limits['cpu_percent'])
                   and runtime.get('KillMode') == 'control-group')
    owned_image = False
    if allocation is not None:
        try:
            metadata = (root / 'runtime' / 'worker.ext4').lstat()
            owned_image = all(getattr(metadata, 'st_' + key) == allocation[key]
                              for key in ('device', 'inode', 'uid', 'gid', 'mode', 'nlink')
                              if key not in ('device', 'inode'))
            owned_image = owned_image and metadata.st_dev == allocation['device'] and metadata.st_ino == allocation['inode']
        except FileNotFoundError:
            pass
    verified = (state['phase'] == 'clean' and absent or state['phase'] in ('registered', 'running')
                and matched and bounded and owned_image)
    return {'phase': state['phase'], 'runtime_absent': absent, 'owned_process_count': owned,
            'limits_verified': bounded, 'allocation_identity_verified': owned_image, 'verified': bool(verified)}
