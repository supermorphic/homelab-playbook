"""Trusted synthetic probe inside the preliminary host resource image."""

import errno
import json
import os
from pathlib import Path
import signal
import socket
import sys


def bounded_limits(readings, limits):
    """Require effective kernel values before any resource exhaustion."""
    try:
        quota, period = map(int, readings['cpu.max'].split())
        return (int(readings['memory.max']) == limits['memory_bytes']
                and int(readings['memory.swap.max']) == 0
                and int(readings['pids.max']) == limits['pids']
                and 0 < quota * 100 <= limits['cpu_percent'] * period)
    except (KeyError, ValueError, TypeError):
        return False


def unprivileged_worker(status, uid, gid):
    try:
        return (list(map(int, status['Uid'].split())) == [uid] * 4
                and list(map(int, status['Gid'].split())) == [gid] * 4
                and not status['Groups'].split() and status['NoNewPrivs'] == '1'
                and all(int(status[key], 16) == 0 for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')))
    except (KeyError, ValueError, TypeError):
        return False


def probe(arguments):
    limits = arguments['limits']
    if os.getuid() != arguments['uid'] or os.getgid() != arguments['gid']:
        raise ValueError('Worker identity differs')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
    if not unprivileged_worker({key: value.strip() for key, value in status.items()},
                               arguments['uid'], arguments['gid']):
        raise ValueError('Probe identity or privileges were not dropped')
    group = Path('/proc/self/cgroup').read_text().strip()
    if not group.startswith('0::/system.slice/') or '\n' in group or '..' in group.split('/'):
        raise ValueError('Unexpected ancestor cgroup')
    cgroup = Path('/sys/fs/cgroup' + group[3:])
    names = ('memory.max', 'memory.swap.max', 'pids.max', 'cpu.max')
    for name in names:
        path = cgroup / name
        if path.stat().st_uid != 0 or os.access(path, os.W_OK):
            raise ValueError('Worker can alter its outer limits')
    if not bounded_limits({name: (cgroup / name).read_text().strip() for name in names}, limits):
        raise ValueError('Effective bounds do not match the experiment')
    if (os.stat('/proc/self/ns/net').st_ino == arguments['host_net']
            or {name for _, name in socket.if_nameindex()} != {'lo'}):
        raise ValueError('Worker namespace is not private')
    # Actual loopback connection in this namespace; never the host's loopback.
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        server.settimeout(2)
        with socket.create_connection(server.getsockname(), timeout=2) as client:
            connection, _ = server.accept()
            with connection:
                client.sendall(b'fixture')
                if connection.recv(7) != b'fixture':
                    raise ValueError('Private loopback failed')
    filesystem = os.statvfs('/work')
    if filesystem.f_blocks * filesystem.f_frsize > limits['disk_bytes']:
        raise ValueError('Worker disk is not independently bounded')
    exhausted = False
    with open('/work/space', 'xb') as stream:
        for offset in range(0, limits['disk_bytes'] + 4 * 1024**2, 4 * 1024**2):
            try:
                os.posix_fallocate(stream.fileno(), offset, 4 * 1024**2)
            except OSError as error:
                if error.errno != errno.ENOSPC:
                    raise
                exhausted = True
                break
    if not exhausted:
        raise ValueError('Worker disk exhaustion was not established')
    # Confirm a kernel pids.max event, not an unrelated per-user limit. Every
    # child is independently observed in the same bounded ancestor cgroup.
    before = int(dict(line.split() for line in (cgroup / 'pids.events').read_text().splitlines())['max'])
    children = []
    pids_bounded = False
    try:
        for _ in range(limits['pids'] + 1):
            try:
                child = os.fork()
            except OSError as error:
                if error.errno != errno.EAGAIN:
                    raise
                after = int(dict(line.split() for line in (cgroup / 'pids.events').read_text().splitlines())['max'])
                pids_bounded = after > before
                break
            if child == 0:
                signal.pause()
                os._exit(0)
            children.append(child)
            if Path(f'/proc/{child}/cgroup').read_text().strip() != group:
                raise ValueError('Child escaped its ancestor cgroup')
    finally:
        for child in children:
            os.kill(child, signal.SIGKILL)
        for child in children:
            os.waitpid(child, 0)
    if not pids_bounded:
        raise ValueError('Process exhaustion was not established')
    return {'limits_verified': True, 'private_loopback': True,
            'disk_bounded': True, 'pids_bounded': True}


if __name__ == '__main__':
    print(json.dumps(probe(json.loads(sys.argv[1]))))
