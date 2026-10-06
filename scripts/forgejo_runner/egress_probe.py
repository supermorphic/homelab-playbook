"""Real DNS, authenticated HTTPS and bounded downloads inside the worker."""

import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import urllib.request

from host_egress import EGRESS_DENIED, FORGEJO_HOST
from host_network import ALLOWED, DENIED, check_connections
from offline_probe import main as worker_probe, execute


def public_connections():
    if not socket.getaddrinfo(FORGEJO_HOST, 443, type=socket.SOCK_STREAM):
        raise ValueError('Forgejo DNS resolution failed')
    context = ssl.create_default_context(cafile='/work/share/public-ca.pem')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       urllib.request.HTTPSHandler(context=context))
    with opener.open('https://' + FORGEJO_HOST + '/api/healthz', timeout=5) as response:
        body = response.read(16385)
        if response.status != 200 or len(body) > 16384 or json.loads(body)['status'] != 'pass':
            raise ValueError('Authenticated Forgejo service is not healthy')
    for scheme in ('http', 'https'):
        with opener.open(scheme + '://deb.debian.org/debian/dists/trixie/InRelease', timeout=5) as response:
            if response.status != 200 or not response.read(512):
                raise ValueError('Public package download access failed')


def main():
    result = worker_probe()
    shutil.copyfile('/etc/ssl/certs/ca-certificates.crt', '/work/share/public-ca.pem')
    Path('/work/share/public-ca.pem').chmod(0o444)
    allowed = [{'address': address, 'port': port} for address, port in ALLOWED]
    denied = [{'address': address, 'port': port} for address, port in DENIED + EGRESS_DENIED]
    check_connections(allowed, denied)
    public_connections()
    deletion = subprocess.run(['/usr/sbin/nft', 'delete', 'table', 'inet', 'forgejo_fixture'],
                              capture_output=True, text=True, timeout=5, check=False)
    if deletion.returncode == 0:
        raise ValueError('Worker removed trusted public-egress policy')
    worker = json.loads(Path('/worker.json').read_text())['worker']
    client = ['/usr/bin/podman', '--remote', '--url=unix:/run/user/' + str(worker['uid']) + '/podman.sock']
    source = Path('/host_network.py').read_text()
    public_source = Path('/egress_probe.py').read_text().split('\ndef main():', 1)[0]
    # Sibling images contain no extra modules. Supply only this fixed trusted
    # connection probe, with its own imports and the independently created CA file.
    imports = public_source[public_source.index('def public_connections():'):]
    code = ('import json,socket,ssl,subprocess,urllib.request\nFORGEJO_HOST=' + repr(FORGEJO_HOST) + '\n'
            + source + '\n' + imports + '\ncheck_connections(' + repr(allowed) + ', ' + repr(denied)
            + ')\npublic_connections()\n')
    for name in ('worker-sibling-one', 'worker-published-http'):
        execute(client + ['exec', name, '/usr/bin/python3', '-c', code])
    return {**result, 'allowed_ipv4': True, 'allowed_ipv6': True,
            'denied_ipv4': True, 'denied_ipv6': True, 'policy_immutable': True,
            'alternate_network_modes': True, 'dns': True, 'forgejo_https': True,
            'public_http': True, 'public_https': True}


if __name__ == '__main__':
    print(json.dumps(main()), flush=True)
