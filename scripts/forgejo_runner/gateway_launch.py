"""Trusted namespace-file handoff inside the owned image and PID namespace."""

import json
import os
import subprocess
import sys

if __package__:
    from .host_egress import pasta_arguments
else:
    from host_egress import pasta_arguments


def prepare_namespace(descriptor):
    expected = os.fstat(descriptor).st_ino
    if expected == os.stat('/proc/self/ns/net').st_ino:
        raise ValueError('Gateway target must be the private worker namespace')
    path = '/work/gateway.netns'
    marker = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(marker)
    # The trusted parent remains alive and pins this descriptor while mount
    # reads it. Do not depend on a helper preserving inherited descriptors.
    source = '/proc/' + str(os.getpid()) + '/fd/' + str(descriptor)
    for command in (['/usr/bin/mount', '--bind', source, path],
                    ['/usr/bin/mount', '-o', 'remount,bind,ro,nosuid,nodev,noexec', path]):
        subprocess.run(command, check=True, capture_output=True, timeout=10)
    if os.stat(path).st_ino != expected:
        raise ValueError('Owned namespace mount identity changed')


def main():
    if os.geteuid() != 0 or len(sys.argv) != 4 or len(sys.argv[2]) > 16384:
        raise ValueError('Fixed gateway handoff requires trusted setup')
    descriptor = int(sys.argv[1])
    uid, gid = map(int, sys.argv[3].split(':'))
    argv = pasta_arguments(json.loads(sys.argv[2]), {'uid': uid, 'gid': gid}, descriptor)
    prepare_namespace(descriptor)
    os.execv(argv[0], argv)


if __name__ == '__main__':
    main()
