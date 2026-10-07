"""Real offline rootless sibling operations inside the temporary worker image."""

import json
from http.client import HTTPConnection
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


def published_port(bindings):
    try:
        if set(bindings) != {'8080/tcp'} or len(bindings['8080/tcp']) != 1:
            raise ValueError
        binding = bindings['8080/tcp'][0]
        value = binding['HostPort']
        if (binding['HostIp'] != '127.0.0.1' or not isinstance(value, str)
                or not value.isascii() or not value.isdigit() or not 1024 <= int(value) <= 65535):
            raise ValueError
        return int(value)
    except (TypeError, KeyError, ValueError) as error:
        raise ValueError('Published HTTP port is missing, ambiguous or outside loopback') from error


def validate_namespace_credentials(status, last_cap):
    mask = (1 << (last_cap + 1)) - 1
    if (list(map(int, status['Uid'].split())) != [0] * 4
            or list(map(int, status['Gid'].split())) != [0] * 4
            or any(int(status[key], 16) != mask for key in ('CapEff', 'CapPrm', 'CapBnd'))):
        raise ValueError('Mapped runtime namespace capabilities were filtered: ' + json.dumps(status))


def check_published_http(bindings):
    port = published_port(bindings)
    connection = HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        connection.request('GET', '/health')
        response = connection.getresponse()
        body = response.read(1025)
        if (response.status != 200 or len(body) > 1024
                or json.loads(body) != {'fixture': 'published-loopback'}):
            raise ValueError('Published sibling HTTP response did not match the owned fixture')
    finally:
        connection.close()


def sibling_lifetime(limits):
    seconds = limits['job_seconds']
    if type(seconds) is not int or not 0 < seconds <= 10800:
        raise ValueError('Invalid synthetic sibling lifetime')
    return seconds + 30


def validate_storage_driver(store, configuration):
    worker=configuration['worker']
    expected=configuration.get('storage_driver','vfs')
    options=store.get('graphOptions',{})
    if (store.get('graphDriverName') != expected
            or store.get('graphRoot') != '/work/graph'
            or store.get('runRoot') != f"/run/user/{worker['uid']}/containers"
            or not isinstance(options,dict)
            or expected == 'overlay' and options.get('overlay.mount_program')):
        raise ValueError('Worker storage differs from its selected owned native driver')


def main():
    config = json.loads(Path('/worker.json').read_text())
    worker = config['worker']
    lifetime = sibling_lifetime(config['limits'])
    runtime = Path(f'/run/user/{worker["uid"]}')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    validate_credentials(status, worker['uid'], worker['gid'])
    filesystem = os.statvfs('/work')
    if filesystem.f_blocks * filesystem.f_frsize > config['limits']['disk_bytes']:
        raise ValueError('Worker writable storage is unbounded')
    execute(['systemd-run', '--user', '--quiet', '--unit=podman-api', '--property=Delegate=yes',
             '--property=StandardOutput=null', '--property=StandardError=file:/work/api-error.log',
             '/usr/bin/podman', 'system', 'service', '--time=0', 'unix:' + str(runtime / 'podman.sock')])
    endpoint = runtime / 'podman.sock'
    deadline = time.monotonic() + 15
    while not endpoint.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Worker Podman API did not become available')
        time.sleep(0.1)
    client = ['/usr/bin/podman', '--remote', '--url=unix:' + str(endpoint)]
    info = json.loads(execute(client + ['info', '--format=json']))
    validate_mappings(info['host'].get('idMappings', {}), worker)
    validate_storage_driver(info['store'],config)
    if (info['host']['security']['rootless'] is not True or info['host']['cgroupVersion'] != 'v2'
            or info['host']['cgroupManager'] != 'systemd'):
        raise ValueError('Worker server lacks rootless delegated cgroups')
    namespace_status = json.loads(execute(['/usr/bin/podman', 'unshare', '/usr/bin/python3', '-c',
        "import json,pathlib; s=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line); print(json.dumps({k:s[k] for k in ('Uid','Gid','CapEff','CapPrm','CapBnd')}))"]))
    validate_namespace_credentials(namespace_status, int(Path('/proc/sys/kernel/cap_last_cap').read_text()))
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
    server = f"import pathlib,socket,time; pathlib.Path('/work/share/result').write_text('owned-sibling'); s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); pathlib.Path('/work/share/port').write_text(str(s.getsockname()[1])); c,a=s.accept(); c.sendall(b'private-loopback'); c.close(); time.sleep({lifetime})"
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
                     'localhost/worker-synthetic:fixture', '/usr/bin/python3', '-c', f'import time; time.sleep({lifetime})']).strip()
    http_server = """from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b'{"fixture":"published-loopback"}')
    def log_message(self, *args):
        pass
HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
"""
    third = execute(client + common + ['--publish=127.0.0.1::8080', '--name=worker-published-http',
                    'localhost/worker-synthetic:fixture', '/usr/bin/python3', '-c', http_server]).strip()
    inspect = json.loads(execute(client + ['inspect', first, second, third]))
    if len(inspect) != 3 or any(item['State']['Running'] is not True for item in inspect):
        raise ValueError('Sibling runtime failed')
    deadline = time.monotonic() + 10
    while True:
        try:
            check_published_http(inspect[2]['NetworkSettings']['Ports'])
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError('Published loopback sibling did not become reachable')
            time.sleep(0.1)
    # Keep siblings and network helpers alive for independent host observation.
    return {'rootless_api': True, 'sibling_containers': True, 'mapped_bind': True,
            'private_loopback': True, 'published_loopback': True,
            'user_manager': True, 'bounded_storage': True}


if __name__ == '__main__':
    print(json.dumps(main()), flush=True)
