"""Real offline rootless sibling operations inside the temporary worker image."""

import json
import os
from pathlib import Path
import socket
import subprocess
import tarfile
import time


def validate_credentials(status, uid, gid):
    if (list(map(int, status['Uid'].split())) != [uid] * 4
            or list(map(int, status['Gid'].split())) != [gid] * 4
            or status['Groups'].strip()
            or any(int(status[key], 16) for key in ('CapEff', 'CapPrm', 'CapInh', 'CapAmb'))
            or int(status['CapBnd'], 16) != 0xc0 or status['NoNewPrivs'].strip() != '0'):
        raise ValueError('Worker credential drop or bounded helper privilege failed')


def execute(argv):
    result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=15)
    if result.returncode:
        # Kept only in the outer root-owned private diagnostic stream.
        raise RuntimeError('Worker operation failed: ' + result.stderr[:4096])
    return result.stdout


def validate_mappings(mappings, worker):
    for kind, identity, start in (('uidmap', 'uid', 'subuid_start'), ('gidmap', 'gid', 'subgid_start')):
        expected = [{'container_id': 0, 'host_id': worker[identity], 'size': 1},
                    {'container_id': 1, 'host_id': worker[start], 'size': worker['subid_count']}]
        if mappings.get(kind) != expected:
            raise ValueError('Worker subordinate mappings are incomplete: ' + json.dumps(mappings))


def main():
    config = json.loads(Path('/worker.json').read_text())
    worker = config['worker']
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    validate_credentials(status, worker['uid'], worker['gid'])
    filesystem = os.statvfs('/work')
    if filesystem.f_blocks * filesystem.f_frsize > config['limits']['disk_bytes']:
        raise ValueError('Worker writable storage is unbounded')
    execute(['systemd-run', '--user', '--quiet', '--unit=podman-api', '--property=Delegate=yes',
             '--property=StandardOutput=null', '--property=StandardError=file:/work/api-error.log',
             '/usr/bin/podman', 'system', 'service', '--time=0', 'unix:/work/run/podman.sock'])
    endpoint = Path('/work/run/podman.sock')
    deadline = time.monotonic() + 15
    while not endpoint.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Worker Podman API did not become available')
        time.sleep(0.1)
    client = ['/usr/bin/podman', '--remote', '--url=unix:/work/run/podman.sock']
    info = json.loads(execute(client + ['info', '--format=json']))
    validate_mappings(info['host'].get('idMappings', {}), worker)
    if (info['host']['security']['rootless'] is not True or info['host']['cgroupVersion'] != 'v2'
            or info['host']['cgroupManager'] != 'systemd'):
        raise ValueError('Worker server lacks rootless delegated cgroups')
    # Generated scratch image only. It executes stock read-only /usr binaries;
    # no registry, credential, production image or application volume is used.
    archive = Path('/work/rootfs.tar')
    with tarfile.open(archive, 'w') as stream:
        for name in ('usr', 'proc', 'sys', 'dev', 'etc', 'work', 'tmp', 'run'):
            entry = tarfile.TarInfo(name); entry.type = tarfile.DIRTYPE; entry.mode = 0o755
            stream.addfile(entry)
        for name in ('bin', 'sbin', 'lib', 'lib64'):
            entry = tarfile.TarInfo(name); entry.type = tarfile.SYMTYPE; entry.linkname = 'usr/' + name
            stream.addfile(entry)
    execute(client + ['import', str(archive), 'localhost/worker-synthetic:fixture'])
    share = Path('/work/share'); share.mkdir(mode=0o777); share.chmod(0o777)
    # UID1 exercises a subordinate host ID, :U ownership and a same-path bind.
    server = "import pathlib,socket,time; pathlib.Path('/work/share/result').write_text('owned-sibling'); s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); pathlib.Path('/work/share/port').write_text(str(s.getsockname()[1])); c,a=s.accept(); c.sendall(b'private-loopback'); c.close(); time.sleep(90)"
    common = ['run', '-d', '--security-opt=no-new-privileges', '-v', '/usr:/usr:ro,nosuid,nodev',
              '-v', '/work/share:/work/share:U', '--user=1:1']
    first = execute(client + common + ['--network=host', '--name=worker-sibling-one',
                    'localhost/worker-synthetic:fixture', '/usr/bin/python3', '-c', server]).strip()
    deadline = time.monotonic() + 15
    while not (share / 'port').exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Private loopback sibling did not start')
        time.sleep(0.1)
    with socket.create_connection(('127.0.0.1', int((share / 'port').read_text())), timeout=5) as connection:
        if connection.recv(64) != b'private-loopback':
            raise ValueError('Sibling loopback was not reachable')
    if (share / 'result').read_text() != 'owned-sibling' or (share / 'result').stat().st_uid != worker['subuid_start']:
        raise ValueError('Sibling bind ownership does not match subordinate mapping')
    if execute(client + ['exec', first, '/usr/bin/python3', '-c',
                         "import pathlib; print(pathlib.Path('/work/share/result').read_text())"]).strip() != 'owned-sibling':
        raise ValueError('Sibling exec or bind path failed')
    execute(client + ['cp', first + ':/work/share/result', '/work/copied'])
    if Path('/work/copied').read_text() != 'owned-sibling':
        raise ValueError('Sibling copy failed')
    second = execute(client + common + ['--network=none', '--name=worker-sibling-two',
                     'localhost/worker-synthetic:fixture', '/usr/bin/python3', '-c', 'import time; time.sleep(90)']).strip()
    inspect = json.loads(execute(client + ['inspect', first, second]))
    if len(inspect) != 2 or any(item['State']['Running'] is not True for item in inspect):
        raise ValueError('Sibling runtime failed')
    # Keep both siblings alive for the independent host ancestry observation.
    print(json.dumps({'rootless_api': True, 'sibling_containers': True, 'mapped_bind': True,
                      'private_loopback': True, 'user_manager': True, 'bounded_storage': True}), flush=True)


if __name__ == '__main__':
    main()
