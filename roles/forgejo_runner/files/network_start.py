"""Apply the accepted egress policy inside the private worker namespace."""

import os
from pathlib import Path
import time

from host_egress import public_policy
from network_setup import command


def configure(configuration):
    expected = configuration['host_network_inode']
    if type(expected) is not int or expected <= 0 or os.stat('/proc/self/ns/net').st_ino == expected:
        raise ValueError('Worker network is not private')
    policy = public_policy(configuration['egress'])
    command(['/usr/sbin/nft', '--check', '--file', '-'], source=policy)
    command(['/usr/sbin/nft', '--file', '-'], source=policy)
    Path('/work/network-ready').write_text('controlled-network-ready\n')
    Path('/work/network-ready').chmod(0o444)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            if Path('/work/egress-ready').read_text() == 'public-gateway-ready\n':
                return None
        except FileNotFoundError:
            pass
        time.sleep(0.1)
    raise TimeoutError('Trusted worker transport startup deadline expired')
