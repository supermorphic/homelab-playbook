"""Start the owned API and load the approved image before one-job admission."""

import json
import os
from pathlib import Path
import subprocess
import time

from worker_probe import validate_credentials, validate_mappings, validate_storage_driver


def command(arguments, *, timeout=15):
    result = subprocess.run(arguments, capture_output=True, text=True, check=False, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Worker startup failed')
    return result.stdout


def main():
    configuration = json.loads(Path('/worker.json').read_text())
    worker = configuration['worker']
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    validate_credentials(status, worker['uid'], worker['gid'])
    if os.statvfs('/work').f_blocks * os.statvfs('/work').f_frsize > configuration['limits']['disk_bytes']:
        raise ValueError('Worker storage is not bounded')
    runtime = Path('/run/user/' + str(worker['uid']))
    endpoint = runtime / 'podman.sock'
    command(['systemd-run', '--user', '--quiet', '--unit=podman-api', '--property=Delegate=yes',
             '--property=StandardOutput=null', '--property=StandardError=null',
             '/usr/bin/podman', 'system', 'service', '--time=0', 'unix:' + str(endpoint)])
    deadline = time.monotonic() + 15
    while not endpoint.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Worker API startup deadline expired')
        time.sleep(0.1)
    controller = configuration['network']['controller']
    command(['/usr/bin/setfacl', '--modify', 'u:' + str(controller['uid']) + ':x', str(runtime)])
    command(['/usr/bin/setfacl', '--modify', 'u:' + str(controller['uid']) + ':rw', str(endpoint)])
    client = ['/usr/bin/podman', '--remote', '--url=unix:' + str(endpoint)]
    info = json.loads(command(client + ['info', '--format=json']))
    validate_mappings(info['host'].get('idMappings', {}), worker)
    validate_storage_driver(info['store'], configuration)
    if (info['host']['security']['rootless'] is not True or info['host']['cgroupVersion'] != 'v2'
            or info['host']['cgroupManager'] != 'systemd'):
        raise ValueError('Worker runtime lacks the accepted rootless capability')
    command(['/usr/bin/podman', 'load', '--input=/work/input/job.oci'], timeout=180)
    identity = configuration['job_image_id']
    image = json.loads(command(client + ['image', 'inspect', identity]))[0]
    if image.get('Id', '').removeprefix('sha256:') != identity or image.get('Architecture') != 'amd64':
        raise ValueError('Worker loaded a different job image')
    command(client + ['tag', identity, 'localhost/forgejo-job:approved'])
    return {'ready': True}


if __name__ == '__main__':
    print(json.dumps(main()), flush=True)
