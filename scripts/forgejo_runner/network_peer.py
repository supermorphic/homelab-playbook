"""Fixed reachable synthetic peer; no worker-supplied code or application state."""

import ctypes
import json
import os
from pathlib import Path
import select
import socket
import sys

from host_network import ALLOWED, DENIED
from network_setup import command


def main():
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
    for address, port in ALLOWED + DENIED:
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        listener = socket.socket(family)
        if family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        listener.bind((address, port)); listener.listen(8)
        listeners.append(listener)
    # Trusted setup ends here. Only synthetic sockets remain under an unused
    # identity, without capability to change policy or acquire execution privilege.
    controller = json.loads(Path('/worker.json').read_text())['network']['controller']
    library = ctypes.CDLL(None, use_errno=True)
    last = int(Path('/proc/sys/kernel/cap_last_cap').read_text())
    if not 0 <= last <= 63:
        raise ValueError('Unexpected kernel capability range')
    for capability in range(last + 1):
        if library.prctl(24, capability, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Cannot clear peer bounding capabilities')
    if library.prctl(47, 4, 0, 0, 0) != 0 or library.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot clear peer execution privilege')
    os.setgroups([]); os.setgid(controller['gid']); os.setuid(controller['uid'])
    print('listeners-ready', flush=True)
    while True:
        readable, _, _ = select.select(listeners, [], [], 1)
        for listener in readable:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(0.2)
                connection.sendall(b'owned-network-endpoint\n')


if __name__ == '__main__':
    main()
