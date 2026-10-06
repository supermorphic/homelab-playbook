"""Real user-service and alternate sibling network modes test the outer policy."""

import json
from pathlib import Path
import subprocess

from host_network import ALLOWED, DENIED, check_connections
from offline_probe import main as worker_probe, execute


def main():
    result = worker_probe()
    allowed = [{'address': address, 'port': port} for address, port in ALLOWED]
    denied = [{'address': address, 'port': port} for address, port in DENIED]
    check_connections(allowed, denied)
    deletion = subprocess.run(['/usr/sbin/nft', 'delete', 'table', 'inet', 'forgejo_fixture'],
                              capture_output=True, text=True, timeout=5, check=False)
    if deletion.returncode == 0:
        raise ValueError('Worker replaced its administrator-owned network policy')
    worker = json.loads(Path('/worker.json').read_text())['worker']
    client = ['/usr/bin/podman', '--remote', '--url=unix:/run/user/' + str(worker['uid']) + '/podman.sock']
    source = Path('/host_network.py').read_text()
    code = source + '\ncheck_connections(' + repr(allowed) + ', ' + repr(denied) + ')\n'
    for name in ('worker-sibling-one', 'worker-published-http'):
        execute(client + ['exec', name, '/usr/bin/python3', '-c', code])
    return {**result, 'allowed_ipv4': True, 'allowed_ipv6': True,
            'denied_ipv4': True, 'denied_ipv6': True,
            'policy_immutable': True, 'alternate_network_modes': True}


if __name__ == '__main__':
    print(json.dumps(main()), flush=True)
