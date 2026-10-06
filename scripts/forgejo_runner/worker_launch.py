"""Fixed image-local setup; drop all active root privilege before the user manager."""

import json
import os
from pathlib import Path
import subprocess


def main():
    configuration = json.loads(Path('/worker.json').read_text())
    worker = configuration['worker']
    if os.geteuid() != 0:
        raise ValueError('The fixed worker launcher requires trusted setup authority')
    if os.getpid() != 1:
        raise ValueError('Private PID isolation is missing; no worker was started')
    # Permit file capabilities only on the owned read-only mapping helpers.
    # Every other /usr executable and the disposable image stay nosuid.
    for helper in ('newuidmap', 'newgidmap'):
        path = '/usr/bin/' + helper
        subprocess.run(['/usr/bin/mount', '--bind', path, path], check=True, timeout=10)
    subprocess.run(['/usr/bin/mount', '-o', 'remount,bind,ro,nosuid,nodev', '/usr'],
                   check=True, timeout=10)
    for helper in ('newuidmap', 'newgidmap'):
        subprocess.run(['/usr/bin/mount', '-o', 'remount,bind,ro,suid,nodev', '/usr/bin/' + helper],
                       check=True, timeout=10)
    if 'network' in configuration:
        from network_setup import configure
        # Keep the child handle until exec; the whole owned unit disposes of it.
        _peer = configure(configuration['network'])
    Path('/run/systemd/system').mkdir(parents=True, exist_ok=True)
    # Stock network-helper confinement permits the standard per-user runtime
    # prefix. Its writable contents still belong to the bounded private image.
    runtime = f'/run/user/{worker["uid"]}'
    Path(runtime).mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run(['/usr/bin/mount', '--bind', '/work/run', runtime], check=True, timeout=10)
    root = Path('/sys/fs/cgroup')
    # Delegate child creation and migration only. Outer resource control files
    # remain root-owned and inaccessible to the execution UID.
    for path in (root, root / 'cgroup.procs', root / 'cgroup.threads', root / 'cgroup.subtree_control'):
        os.chown(path, worker['uid'], worker['gid'])
    os.environ['USER'] = worker['user']
    os.environ['LOGNAME'] = worker['user']
    os.environ['XDG_RUNTIME_DIR'] = runtime
    os.environ['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + runtime + '/bus'
    os.execv('/usr/bin/setpriv', ['/usr/bin/setpriv', f'--reuid={worker["uid"]}',
        f'--regid={worker["gid"]}', '--clear-groups', '--bounding-set=-all,+setuid,+setgid',
        '--inh-caps=-all', '--ambient-caps=-all', '/usr/bin/catatonit', '--',
        '/usr/lib/systemd/systemd', '--user',
        '--log-target=console', '--log-level=warning'])


if __name__ == '__main__':
    main()
