"""Fixed reachable synthetic peer; no worker-supplied code or application state."""

import json
import os
from pathlib import Path
import select
import socket
import sys

if __package__:
    from .host_network import ALLOWED, DENIED
    from .network_setup import command
else:
    from host_network import ALLOWED, DENIED
    from network_setup import command


def main():
    configuration = json.loads(Path('/worker.json').read_text())['network']
    endpoints = ALLOWED + DENIED
    if 'egress' in configuration:
        from host_egress import EGRESS_DENIED
        endpoints += EGRESS_DENIED
    if len(sys.argv) == 3 and sys.argv[1] == '--serve':
        descriptors = [int(value) for value in sys.argv[2].split(',')]
        if len(descriptors) != len(endpoints) or len(set(descriptors)) != len(descriptors) or min(descriptors) < 3:
            raise ValueError('Invalid controlled listener handoff')
        listeners = [socket.socket(fileno=descriptor) for descriptor in descriptors]
        endpoints = {(listener.getsockname()[0], listener.getsockname()[1]) for listener in listeners}
        if endpoints != set(ALLOWED + DENIED + (EGRESS_DENIED if 'egress' in configuration else ())):
            raise ValueError('Listener identity changed across peer handoff')
        print('listeners-ready', flush=True)
        while True:
            readable, _, _ = select.select(listeners, [], [], 1)
            for listener in readable:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(0.2)
                    connection.sendall(b'owned-network-endpoint\n')
    if len(sys.argv) != 1 or os.geteuid() != 0:
        raise ValueError('Controlled peer preparation requires fixed trusted setup')
    print('namespace-ready', flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], 10)
    if not ready or sys.stdin.readline(32) != 'configure\n':
        raise ValueError('Controlled peer handoff timed out')
    command(['/usr/sbin/ip', 'link', 'set', 'lo', 'up'])
    command(['/usr/sbin/ip', 'address', 'add', '10.57.0.2/24', 'dev', 'fixturepeer'])
    command(['/usr/sbin/ip', '-6', 'address', 'add', 'fd57::2/64', 'dev', 'fixturepeer', 'nodad'])
    command(['/usr/sbin/ip', 'link', 'set', 'fixturepeer', 'up'])
    command(['/usr/sbin/ip', 'address', 'add', '192.0.2.20/32', 'dev', 'lo'])
    command(['/usr/sbin/ip', '-6', 'address', 'add', '2001:db8:57::20/128', 'dev', 'lo', 'nodad'])
    listeners = []
    for address, port in endpoints:
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        listener = socket.socket(family)
        if family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        listener.bind((address, port)); listener.listen(8)
        listener.set_inheritable(True)
        listeners.append(listener)
    # Trusted setup ends here. Only synthetic sockets remain under an unused
    # identity, without capability to change policy or acquire execution privilege.
    controller = configuration['controller']
    os.execv('/usr/bin/setpriv', ['/usr/bin/setpriv', f'--reuid={controller["uid"]}',
        f'--regid={controller["gid"]}', '--clear-groups', '--bounding-set=-all',
        '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs',
        '/usr/bin/python3', '/network_peer.py', '--serve',
        ','.join(str(listener.fileno()) for listener in listeners)])


if __name__ == '__main__':
    main()
