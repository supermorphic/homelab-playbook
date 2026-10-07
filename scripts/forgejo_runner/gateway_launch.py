"""Trusted TAP handoff inside the owned image, without runtime namespace authority."""

import fcntl
import json
import os
import struct
import subprocess
import sys

if __package__:
    from .host_egress import TRANSPORT_MTU, transport_arguments
else:
    from host_egress import TRANSPORT_MTU, transport_arguments


def prepare_tap(network, host):
    expected = os.fstat(network).st_ino
    host_inode = os.stat('/proc/self/ns/net').st_ino
    if expected == host_inode or os.fstat(host).st_ino != host_inode:
        raise ValueError('Gateway target must be the private worker namespace')
    tap = None
    try:
        os.setns(network, os.CLONE_NEWNET)
        if os.stat('/proc/self/ns/net').st_ino != expected:
            raise ValueError('Private network handoff changed')
        tap = os.open('/dev/net/tun', os.O_RDWR | os.O_CLOEXEC)
        # Linux TUNSETIFF, IFF_TAP | IFF_NO_PI. Only this private interface
        # descriptor crosses the privilege handoff, never a namespace handle.
        fcntl.ioctl(tap, 0x400454ca, struct.pack('16sH', b'uplink0', 0x1002))
        commands = (['link', 'set', 'uplink0', 'mtu', str(TRANSPORT_MTU), 'up'],
                    ['address', 'add', '10.57.1.100/24', 'dev', 'uplink0'],
                    ['route', 'add', 'default', 'via', '10.57.1.2', 'dev', 'uplink0'],
                    ['-6', 'address', 'add', 'fd00::100/64', 'dev', 'uplink0', 'nodad'],
                    ['-6', 'route', 'add', 'default', 'via', 'fd00::2', 'dev', 'uplink0'])
        for arguments in commands:
            subprocess.run(['/usr/sbin/ip', *arguments], check=True, capture_output=True, timeout=10)
    except BaseException:
        if tap is not None:
            os.close(tap)
        raise
    finally:
        os.setns(host, os.CLONE_NEWNET)
    if os.stat('/proc/self/ns/net').st_ino != host_inode:
        os.close(tap)
        raise ValueError('Trusted transport did not return to its original network')
    return tap


def main():
    if os.geteuid() != 0 or len(sys.argv) != 5 or len(sys.argv[3]) > 16384:
        raise ValueError('Fixed TAP handoff requires trusted setup')
    network, host = map(int, sys.argv[1:3])
    uid, gid = map(int, sys.argv[4].split(':'))
    configuration = json.loads(sys.argv[3])
    # Validate before opening or configuring a network interface.
    transport_arguments(configuration, 9)
    tap = prepare_tap(network, host)
    os.dup2(tap, 9, inheritable=True)
    if tap != 9:
        os.close(tap)
    # Drop namespace/root/cgroup handles inherited from trusted setup.
    for descriptor in os.listdir('/proc/self/fd'):
        number = int(descriptor)
        if number > 2 and number != 9:
            try:
                os.close(number)
            except OSError:
                pass
    argv = ['/usr/bin/setpriv', f'--reuid={uid}', f'--regid={gid}', '--clear-groups',
            '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs',
            *transport_arguments(configuration, 9)]
    os.execv(argv[0], argv)


if __name__ == '__main__':
    main()
