"""Attended bounded drain and explicit recovery of installed shared worker slots."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from control import load_configuration, perform


def execute(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=330, check=False,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    if result.returncode or len(result.stdout) > 16384:
        raise RuntimeError('Bounded supervisor operation failed')
    return result.stdout.strip()


def drain(value, *, command=execute, control=perform, clock=time.monotonic, sleep=time.sleep):
    name = 'forgejo-runner-' + value['slot']['name']
    command(['systemctl', 'stop', name + '.timer'])
    deadline = clock() + value['slot']['limits']['job_seconds'] + 300
    while clock() < deadline:
        active = command(['systemctl', 'show', '--property=ActiveState', '--value', name + '.service'])
        if active in ('inactive', 'failed'):
            result, code = control('inspect', value)
            if code or not result.get('runtime_absent') or not result.get('verified'):
                raise ValueError('Owned cleanup is incomplete; keep admission stopped and use recovery')
            return {'drained': True}
        if active not in ('active', 'activating', 'deactivating', 'reloading'):
            raise ValueError('Supervisor returned an unknown state')
        sleep(1)
    raise TimeoutError('Drain deadline expired; admission remains stopped')


def recover(value, *, command=execute, control=perform):
    name = 'forgejo-runner-' + value['slot']['name']
    command(['systemctl', 'stop', name + '.timer'])
    command(['systemctl', 'stop', name + '.service'])
    return control('recover', value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('drain', 'recover'),
                        help='drain waits without cancellation; recover explicitly terminates the owned job')
    parser.add_argument('--config', required=True, type=Path)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Installed maintenance requires administrator authority')
    try:
        value = load_configuration(arguments.config)
        if arguments.mode == 'drain':
            result, code = drain(value), 0
        else:
            result, code = recover(value)
    except Exception as error:
        result, code = {'error_type': type(error).__name__, 'admission_stopped': True}, 1
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
