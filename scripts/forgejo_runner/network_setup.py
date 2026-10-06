"""Fixed trusted setup inside the disposable worker's initial-user network namespace."""

import os
import select
import subprocess

if __package__:
    from .host_network import policy_source
else:
    from host_network import policy_source


def command(argv, *, source=None):
    result = subprocess.run(argv, input=source, capture_output=True, text=True,
                            timeout=10, check=False)
    if result.returncode:
        raise RuntimeError('Controlled namespace setup failed: ' + result.stderr[:1024])


def readiness(peer, expected):
    ready, _, _ = select.select([peer.stdout], [], [], 10)
    if not ready or peer.stdout.readline(64) != expected + '\n':
        raise ValueError('Controlled peer setup did not complete within its bound')


def configure(configuration):
    expected = configuration['host_network_inode']
    if type(expected) is not int or expected <= 0 or os.stat('/proc/self/ns/net').st_ino == expected:
        raise ValueError('Private network isolation is missing; no interface was configured')
    # Every network operation remains in the owned unit. It has no host network
    # namespace descriptor or shared host interface.
    peer = subprocess.Popen(['/usr/bin/unshare', '--net', '--', '/usr/bin/python3', '/network_peer.py'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    readiness(peer, 'namespace-ready')
    command(['/usr/sbin/ip', 'link', 'add', 'fixture0', 'type', 'veth', 'peer', 'name', 'fixturepeer'])
    command(['/usr/sbin/ip', 'link', 'set', 'fixturepeer', 'netns', str(peer.pid)])
    command(['/usr/sbin/ip', 'address', 'add', '10.57.0.1/24', 'dev', 'fixture0'])
    command(['/usr/sbin/ip', '-6', 'address', 'add', 'fd57::1/64', 'dev', 'fixture0', 'nodad'])
    command(['/usr/sbin/ip', 'link', 'set', 'fixture0', 'up'])
    command(['/usr/sbin/ip', 'route', 'add', 'default', 'via', '10.57.0.2', 'dev', 'fixture0'])
    command(['/usr/sbin/ip', '-6', 'route', 'add', 'default', 'via', 'fd57::2', 'dev', 'fixture0'])
    command(['/usr/sbin/nft', '--check', '--file', '-'], source=policy_source())
    command(['/usr/sbin/nft', '--file', '-'], source=policy_source())
    peer.stdin.write('configure\n'); peer.stdin.flush()
    readiness(peer, 'listeners-ready')
    return peer
