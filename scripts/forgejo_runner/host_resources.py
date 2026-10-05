"""Owned resources for a bounded, preliminary host experiment.

This stage does not establish Podman, runner or repository workload acceptance.
"""

import os
from pathlib import Path
import stat
import json
import shutil
import subprocess
import time


def identity(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
            metadata.st_mode, 0 if stat.S_ISDIR(metadata.st_mode) else metadata.st_nlink)


class Resources:
    """Remove only freshly matched entries, without traversing worker data."""

    def __init__(self):
        self.entries = []

    def record_file(self, path, *, allow_link=False):
        path = Path(path)
        metadata = path.lstat()
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
                or allow_link and stat.S_ISLNK(metadata.st_mode)):
            raise ValueError('Cannot own a symlink or special file')
        parent = path.parent.resolve(strict=True)
        self.entries.append((parent / path.name, identity(metadata), identity(parent.stat())))

    def cleanup(self):
        errors = []
        # All entries must match before the first removal. An ambiguous image
        # must not be unlinked as a side effect of removing its parent directory.
        try:
            for path, expected, parent in self.entries:
                if identity(path.lstat()) != expected or identity(path.parent.lstat()) != parent:
                    raise ValueError('Resource identity changed; state preserved')
        except (OSError, ValueError):
            return ['Resource identity changed; state preserved']
        for path, expected, parent in reversed(self.entries):
            descriptor = None
            try:
                descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                current = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
                if identity(current) != expected or identity(os.fstat(descriptor)) != parent:
                    raise ValueError('Resource identity changed')
                if stat.S_ISDIR(current.st_mode):
                    os.rmdir(path.name, dir_fd=descriptor)
                else:
                    os.unlink(path.name, dir_fd=descriptor)
            except (OSError, ValueError):
                errors.append('Resource removal failed; remaining state preserved')
                break
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        if not errors:
            self.entries.clear()
        return errors


def run_with_cleanup(primary, cleanup):
    result = {'exit_code': 0, 'primary_error': None, 'cleanup_errors': []}
    try:
        result['observations'] = primary()
    except BaseException as error:
        result['exit_code'] = 130 if isinstance(error, KeyboardInterrupt) else 1
        # Remote diagnostics can contain private paths and identifiers. Keep the
        # primary failure independent of teardown without publishing its text.
        result['primary_error'] = type(error).__name__
    finally:
        try:
            result['cleanup_errors'] = cleanup()
        except BaseException as error:
            result['cleanup_errors'] = [type(error).__name__]
        if result['cleanup_errors']:
            result['exit_code'] = result['exit_code'] or 1
    return result


def validate_probe_budget(target, capacity=None):
    """This preliminary probe has lower limits than future real workload jobs."""
    limits = target['limits']
    ranges = {'memory_bytes': (64 * 1024**2, 512 * 1024**2),
              'disk_bytes': (64 * 1024**2, 512 * 1024**2),
              'pids': (16, 64), 'cpu_percent': (1, 50), 'job_seconds': (5, 60)}
    if any(type(limits[key]) is not int or not lower <= limits[key] <= upper
           for key, (lower, upper) in ranges.items()):
        raise ValueError('Resource probe exceeds its bounded experiment budgets')
    if capacity is not None:
        if (capacity['memory_available'] < limits['memory_bytes'] + 1024**3
                or capacity['memory_total'] < limits['memory_bytes'] * 8
                or capacity['disk_available'] < limits['disk_bytes'] + 2 * 1024**3):
            raise ValueError('Insufficient host reserve; no setup was performed')


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'},
                            check=False)
    if result.returncode:
        # systemctl show uses a nonzero status for an absent unit.
        if argv[:2] != ['systemctl', 'show'] or 'LoadState=not-found' not in result.stdout:
            raise RuntimeError('Host resource command failed')
    return result.stdout


class OwnedUnit:
    """Match an owned transient invocation before stopping its whole cgroup."""

    def __init__(self, name, image, execute=None):
        self.name = name
        self.image = str(image)
        self.execute = execute or command
        self.invocation = None
        self.attempted = False

    def observe(self):
        output = self.execute(['systemctl', 'show',
            '--property=LoadState,InvocationID,RootImage,ActiveState,SubState,ControlGroup',
            '--', self.name])
        return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)

    def claim(self):
        observed = self.observe()
        if observed.get('RootImage') != self.image or not observed.get('InvocationID'):
            raise ValueError('Unit ownership is ambiguous')
        self.invocation = observed['InvocationID']
        self.attempted = True

    def cleanup(self):
        if not self.attempted:
            return []
        try:
            observed = self.observe()
            if observed.get('LoadState') == 'not-found':
                return []
            # A lost startup response can leave a unit before claim(). Exact
            # root-owned image identity binds it to this allocation; mismatches
            # are preserved rather than adopting a same-name foreign unit.
            if observed.get('RootImage') != self.image or not observed.get('InvocationID'):
                raise ValueError('Unit ownership changed')
            if self.invocation is None:
                self.invocation = observed['InvocationID']
            if observed['InvocationID'] != self.invocation:
                raise ValueError('Unit invocation changed')
            self.execute(['systemctl', 'stop', '--', self.name])
            after = self.observe()
            if after.get('LoadState') != 'not-found':
                if (after.get('ActiveState') not in ('inactive', 'failed')
                        or after.get('RootImage') != self.image
                        or after.get('InvocationID') not in ('', self.invocation)):
                    raise ValueError('Unit termination could not be established')
                if after.get('ActiveState') == 'failed':
                    self.execute(['systemctl', 'reset-failed', '--', self.name])
                deadline = time.monotonic() + 5
                while True:
                    after = self.observe()
                    if after.get('LoadState') == 'not-found':
                        break
                    if (after.get('RootImage') != self.image
                            or after.get('InvocationID') not in ('', self.invocation)
                            or after.get('ActiveState') != 'inactive'
                            or time.monotonic() >= deadline):
                        raise ValueError('Owned transient unit metadata survived cleanup')
                    time.sleep(0.1)
            group = observed.get('ControlGroup')
            if group:
                if not group.startswith('/system.slice/') or '..' in group.split('/'):
                    raise ValueError('Unexpected unit cgroup')
                events = Path('/sys/fs/cgroup' + group) / 'cgroup.events'
                if events.exists() and 'populated 1' in events.read_text().splitlines():
                    raise ValueError('Worker processes survived termination')
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return ['Unit cleanup failed; allocation preserved']
        return []


def host_capacity(parent):
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in ('MemTotal', 'MemAvailable'):
            memory[key] = int(value.split()[0]) * 1024
    return {'memory_total': memory['MemTotal'], 'memory_available': memory['MemAvailable'],
            'disk_available': shutil.disk_usage(parent).free}


def active_services():
    output = command(['systemctl', 'list-units', '--type=service', '--state=active',
                      '--plain', '--no-legend', '--no-pager'])
    return {line.split()[0] for line in output.splitlines() if line.strip()}


def image_attached(image):
    """Do not unlink a filesystem still referenced by a kernel loop device."""
    for backing in Path('/sys/block').glob('loop*/loop/backing_file'):
        try:
            value = backing.read_text().strip().removesuffix(' (deleted)')
        except FileNotFoundError:
            continue  # An unrelated loop device was detached during observation.
        if value.lstrip('/') == str(image).lstrip('/'):
            return True
    return False


def run_resource_probe(target, observe, validate, probe_source):
    """Synthetic resource stage; never run a runner, socket or repository code."""
    resources = Resources()
    root = Path(target['state_root'])
    unit = OwnedUnit('forgejo-resource-' + target['ownership_marker'] + '.service', root / 'worker.ext4')
    services = None
    diagnostic = ''

    def cleanup():
        nonlocal diagnostic
        # Validate storage identities before stopping anything or deleting its
        # image. Keep storage when the unit's identity or termination is unclear.
        for path, expected, parent in resources.entries:
            if identity(path.lstat()) != expected or identity(path.parent.lstat()) != parent:
                return ['Resource identity changed; allocation preserved']
        error_log = root.resolve() / 'stderr.log'
        if any(path == error_log for path, _, _ in resources.entries):
            descriptor = os.open(error_log, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'r') as stream:
                # Only the fixed trusted launcher/probe writes this private
                # log. Never forward it to console, CI logs or a public PR.
                diagnostic = stream.read(4096)
        errors = unit.cleanup()
        if not errors:
            deadline = time.monotonic() + 5
            while image_attached(root / 'worker.ext4'):
                if time.monotonic() >= deadline:
                    errors.append('Worker image remains attached; allocation preserved')
                    break
                time.sleep(0.1)
        if not errors:
            errors = resources.cleanup()
        if services is not None and not services <= active_services():
            errors.append('An existing active service changed; inspect host state')
        return errors

    def setup_and_probe():
        nonlocal services
        if os.geteuid() != 0:
            raise ValueError('Administrator fixture access is required')
        validate(target, observe(target), allow_new_fixture=True)
        destination = root.parent if root.parent.exists() else root.parent.parent
        validate_probe_budget(target, host_capacity(destination))
        for tool in ('fallocate', 'mkfs.ext4', 'systemd-run', 'setpriv'):
            if shutil.which(tool) is None:
                raise ValueError('A resource probe prerequisite is missing')
        if command(['systemctl', 'is-system-running']).strip() != 'running':
            raise ValueError('Host system manager is not healthy')
        if unit.observe().get('LoadState') != 'not-found':
            raise ValueError('Resource probe unit already exists')
        services = active_services()
        if not root.parent.exists():
            root.parent.mkdir(mode=0o700)
            resources.record_file(root.parent)
            ownership = root.parent / 'ownership'
            with ownership.open('x') as stream:
                stream.write(target['ownership_marker'] + '\n')
            ownership.chmod(0o600)
            resources.record_file(ownership)
        root.mkdir(mode=0o700)
        resources.record_file(root)
        staging = root / 'rootfs'
        staging.mkdir(mode=0o755)
        resources.record_file(staging)
        for name in ('usr', 'proc', 'sys', 'dev', 'work', 'run', 'tmp', 'etc'):
            path = staging / name
            path.mkdir(mode=0o755)
            if name == 'work':
                os.chown(path, target['worker']['uid'], target['worker']['gid'])
            resources.record_file(path)
        # The trusted probe lives in the image. /usr is mounted read-only by
        # systemd; no host /etc, /home, application paths or sockets are bound.
        for name in ('bin', 'sbin', 'lib', 'lib64'):
            link = staging / name
            link.symlink_to('usr/' + name)
            resources.record_file(link, allow_link=True)
        probe = staging / 'probe.py'
        probe.write_text(probe_source)
        probe.chmod(0o444)
        resources.record_file(probe)
        image = root / 'worker.ext4'
        with image.open('xb'):
            pass
        image.chmod(0o600)
        resources.record_file(image)
        # Recheck the actual destination after setup, immediately before its
        # allocation. An existing fixture parent may be a separate filesystem.
        validate_probe_budget(target, host_capacity(root))
        command(['fallocate', '-l', str(target['limits']['disk_bytes']), str(image)])
        command(['mkfs.ext4', '-q', '-F', '-m', '0', '-d', str(staging), str(image)])
        output = root / 'result.json'
        with output.open('xb'):
            pass
        output.chmod(0o600)
        resources.record_file(output)
        error_log = root / 'stderr.log'
        with error_log.open('xb'):
            pass
        error_log.chmod(0o600)
        resources.record_file(error_log)
        receipt = root / 'receipt.json'
        with receipt.open('x'):
            pass
        receipt.chmod(0o600)
        resources.record_file(receipt)

        def save_receipt():
            receipt.write_text(json.dumps({
                'unit': unit.name, 'image': str(image), 'invocation': unit.invocation,
                'resources': [{'path': str(path), 'identity': expected, 'parent_identity': parent}
                              for path, expected, parent in resources.entries],
            }) + '\n')

        save_receipt()
        limits = target['limits']
        properties = [
            'Type=exec', 'RemainAfterExit=yes', 'KillMode=control-group', 'TimeoutStopSec=5s',
            f'RuntimeMaxSec={limits["job_seconds"]}s',
            f'MemoryMax={limits["memory_bytes"]}', 'MemorySwapMax=0',
            f'TasksMax={limits["pids"]}', f'CPUQuota={limits["cpu_percent"]}%',
            f'RootImage={image}', 'RootImageOptions=nodev,nosuid', 'MountAPIVFS=yes',
            'BindReadOnlyPaths=/usr', 'BindLogSockets=no', 'PrivateNetwork=yes', 'PrivateDevices=yes',
            'ProtectControlGroups=yes', 'ProtectSystem=strict', 'ReadWritePaths=/work',
            'ProtectProc=invisible',
            # Numeric IDs absent from NSS cannot be passed to systemd User=.
            # This fixed trusted launcher drops identity and every capability
            # before Python starts; no account is created or repository run.
            'NoNewPrivileges=yes', 'CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_SETPCAP',
            # systemd drops SETUID while applying its implicit sandbox syscall
            # filter unless it is explicitly retained for the launcher. The
            # launcher clears ambient/bounding capabilities before the probe.
            'AmbientCapabilities=CAP_SETUID',
            'User=0', 'Group=0',
            f'StandardOutput=file:{output}', f'StandardError=file:{error_log}',
            'Environment=HOME=/work TMPDIR=/work XDG_RUNTIME_DIR=/work/run',
        ]
        argv = ['systemd-run', '--quiet', '--unit=' + unit.name]
        for setting in properties:
            argv.append('--property=' + setting)
        argv.extend(['/usr/bin/setpriv', f'--reuid={target["worker"]["uid"]}',
            f'--regid={target["worker"]["gid"]}', '--clear-groups', '--bounding-set=-all',
            '--inh-caps=-all', '--ambient-caps=-all', '/usr/bin/python3', '/probe.py', json.dumps({
            'limits': limits, 'uid': target['worker']['uid'], 'gid': target['worker']['gid'],
            'host_net': os.stat('/proc/self/ns/net').st_ino})])
        unit.attempted = True
        command(argv)
        unit.claim()
        save_receipt()
        deadline = time.monotonic() + limits['job_seconds'] + 5
        while time.monotonic() < deadline:
            current = unit.observe()
            if current.get('InvocationID') != unit.invocation:
                raise ValueError('Unit identity changed during probe')
            if current.get('SubState') == 'exited':
                if output.stat().st_size > 16384:
                    raise ValueError('Resource probe output exceeded its budget')
                observed = json.loads(output.read_text())
                expected = ('limits_verified', 'private_loopback', 'disk_bounded', 'pids_bounded')
                if set(observed) != set(expected) or any(observed[key] is not True for key in expected):
                    raise ValueError('Resource probe did not establish its bounds')
                return observed
            if current.get('ActiveState') in ('failed', 'inactive'):
                raise RuntimeError('Resource probe failed')
            time.sleep(0.1)
        raise RuntimeError('Resource probe deadline exceeded')

    result = run_with_cleanup(setup_and_probe, cleanup)
    if result['exit_code'] and diagnostic:
        result['diagnostic'] = diagnostic
    return {**result, 'acceptance': 'resources-only'}
