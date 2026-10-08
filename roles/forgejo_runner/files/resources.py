"""Deployment storage and systemd contracts promoted from native acceptance."""

from pathlib import Path
import json
import os
import re
import shutil
import stat
import subprocess
import time

from scripts.forgejo_runner.host_resources import Resources, OwnedUnit, identity, image_attached, command, active_services
from scripts.forgejo_runner.host_worker import worker_files, worker_cleanup_errors, read_worker_result
from scripts.forgejo_runner.host_egress import observe_configuration, dns_forward, start_gateway


def unit_properties(config, image, helper_binds):
    limits = config['limits']
    return [
        'Type=exec', 'RemainAfterExit=yes', 'KillMode=control-group', 'TimeoutStopSec=5s', 'LimitCORE=0',
        'RuntimeMaxSec=' + str(limits['job_seconds']) + 's',
        'MemoryMax=' + str(limits['memory_bytes']), 'MemorySwapMax=0',
        'TasksMax=' + str(limits['pids']), 'CPUQuota=' + str(limits['cpu_percent']) + '%',
        'RootImage=' + str(image), 'RootImageOptions=nodev,nosuid', 'MountAPIVFS=yes',
        'BindReadOnlyPaths=/usr ' + ' '.join(helper_binds), 'BindLogSockets=no',
        'PrivateNetwork=yes', 'PrivateDevices=yes', 'PrivatePIDs=yes', 'ProtectProc=default',
        'ProtectSystem=strict', 'ReadWritePaths=/work /controller',
        'ProtectControlGroupsEx=private', 'Delegate=yes', 'NoNewPrivileges=no',
        'BindPaths=/dev/net/tun', 'DeviceAllow=/dev/net/tun rw',
        'CapabilityBoundingSet=CAP_SYS_ADMIN CAP_CHOWN CAP_SETUID CAP_SETGID CAP_SETPCAP CAP_NET_ADMIN',
        'AmbientCapabilities=CAP_SYS_ADMIN CAP_CHOWN CAP_SETUID CAP_SETGID CAP_NET_ADMIN',
        'User=0', 'Group=0', 'StandardOutput=null', 'StandardError=null',
        'Environment=HOME=/work TMPDIR=/work',
    ]


def image_policy(manifest):
    images = manifest.get('registry_images')
    if not isinstance(images, list) or len(images) > 64:
        raise ValueError('Registry policy requires a bounded image allowlist')
    approved = {}
    for reference in images:
        if (not isinstance(reference, str)
                or re.fullmatch(r'[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9/_.-]*(?::[A-Za-z0-9_.-]+)?(?:@sha256:[0-9a-f]{64})?',
                                reference) is None):
            raise ValueError('Registry policy contains an invalid image reference')
        canonical = reference
        if '@' in reference:
            name, digest = reference.split('@')
            prefix, image = name.rsplit('/', 1)
            canonical = prefix + '/' + image.split(':', 1)[0] + '@' + digest
        approved[canonical] = [{'type': 'insecureAcceptAnything'}]
    return {'default': [{'type': 'reject'}], 'transports': {
        'tarball': {'': [{'type': 'insecureAcceptAnything'}]},
        'oci-archive': {'/work/input/job.oci': [{'type': 'insecureAcceptAnything'}]},
        'docker': approved}}


def validate_receipt(config, generation, value):
    if (not isinstance(generation, str) or re.fullmatch(r'[0-9a-f]{32}', generation) is None
            or not isinstance(value, dict)
            or set(value) != {'schema', 'generation', 'unit', 'invocation', 'resources'}
            or type(value['schema']) is not int or value['schema'] != 1
            or value['generation'] != generation
            or value['unit'] != 'forgejo-worker-' + config['name'] + '-' + generation + '.service'
            or value['invocation'] is not None and (
                not isinstance(value['invocation'], str)
                or re.fullmatch(r'[0-9a-f]{32}', value['invocation']) is None)
            or not isinstance(value['resources'], list) or not 1 <= len(value['resources']) <= 256):
        raise ValueError('Invalid owned resource receipt')
    paths = set()
    for row in value['resources']:
        if not isinstance(row, dict) or set(row) != {'path', 'identity', 'parent_identity'}:
            raise ValueError('Invalid owned resource entry')
        relative = row['path']
        if (not isinstance(relative, str) or not relative or relative in paths
                or Path(relative).is_absolute() or '..' in Path(relative).parts
                or str(Path(relative)) != relative):
            raise ValueError('Receipt path is outside its canonical allocation')
        paths.add(relative)
        for key in ('identity', 'parent_identity'):
            identity = row[key]
            if (not isinstance(identity, list) or len(identity) != 6
                    or any(type(part) is not int or not 0 <= part < 2**63 for part in identity)
                    or identity[1] == 0 or stat.S_IFMT(identity[4]) not in (stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK)
                    or key == 'parent_identity' and not stat.S_ISDIR(identity[4])):
                raise ValueError('Receipt lacks valid filesystem identities')
    root = next((row for row in value['resources'] if row['path'] == '.'), None)
    if root is None or root['identity'][2:] != [0, 0, stat.S_IFDIR | 0o700, 0]:
        raise ValueError('Allocation root does not belong to trusted setup')
    return value


def remaining_resources(entries):
    """Reconcile already absent entries without adopting replacement parents."""
    known = {path: (expected, parent) for path, expected, parent in entries}
    def parent_matches(path, expected):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            recorded = known.get(path)
            if recorded is None or recorded[0] != expected or not stat.S_ISDIR(expected[4]):
                raise ValueError('Absent parent lacks an owned identity')
            parent_matches(path.parent, recorded[1])
        else:
            if identity(metadata) != expected:
                raise ValueError('Resource parent identity changed')
    remaining = []
    for path, expected, parent in entries:
        parent_matches(path.parent, parent)
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        if identity(current) != expected:
            raise ValueError('Resource identity changed')
        remaining.append((path, expected, parent))
    return remaining


class OwnedRuntime:
    """Private RootImage with durable ownership receipts and bounded teardown."""

    def __init__(self, config, installation, assets_root, manifest, handle, store):
        self.config = config
        self.installation, self.assets_root = Path(installation), Path(assets_root)
        self.manifest, self.handle, self.store = manifest, handle, store
        self.state_root = Path(config['state_root'])
        self.root = self.state_root / 'runtime'
        self.resources = Resources()
        self.unit = None
        self.generation = None
        self.gateway = {}

    def assert_empty(self):
        from verify import observe
        observed = observe(self.config)
        if not observed['runtime_absent']:
            raise ValueError('Previous runtime or processes remain')
        if os.path.lexists(self.state_root / 'resources.json'):
            raise ValueError('A previous ownership receipt requires recovery')

    def target(self):
        worker = self.config['worker']
        return {**self.config, 'state_root': str(self.root), 'ownership_marker': self.config['name'] + '-' + self.generation,
                'worker': {**worker, 'user': worker['name'], 'subid_count': worker['subuid_count']}}

    def receipt(self):
        return {'schema': 1, 'generation': self.generation, 'unit': self.unit.name,
                'invocation': self.unit.invocation,
                'resources': [{'path': str(path.relative_to(self.root)), 'identity': list(expected),
                               'parent_identity': list(parent)} for path, expected, parent in self.resources.entries]}

    def persist(self):
        value = validate_receipt(self.config, self.generation, self.receipt())
        destination = self.state_root / 'resources.json'
        temporary = self.state_root / ('.resources-' + self.generation)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory = os.open(self.state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def record(self, path, *, allow_link=False):
        self.resources.record_file(path, allow_link=allow_link)
        self.persist()

    def capacity(self):
        from pool import require_capacity
        require_capacity(self.config)

    def prepare(self, generation):
        from scripts.forgejo_runner.deployment_assets import verify, validate_executable
        from scripts.forgejo_runner.host_probe import secure_path
        if os.geteuid() != 0 or self.config['state_root'] != '/var/lib/forgejo-runner/' + self.config['name']:
            raise ValueError('Runtime setup requires the canonical trusted slot boundary')
        if not secure_path(self.state_root) or command(['systemctl', 'is-system-running']).strip() != 'running':
            raise ValueError('The current slot boundary or host manager is unsafe')
        self.assert_empty()
        self.capacity()
        from pool import service_baseline
        self.services = service_baseline(self.config)
        self.generation = generation
        self.unit = OwnedUnit('forgejo-worker-' + self.config['name'] + '-' + generation + '.service',
                              self.root / 'worker.ext4')
        if self.unit.observe().get('LoadState') != 'not-found':
            raise ValueError('A same-name unit already exists')
        for name in ('job.oci', 'forgejo-runner', 'netavark'):
            verify(self.assets_root / name, self.manifest['artifacts'][name])
        validate_executable(self.assets_root / 'forgejo-runner')
        self.root.mkdir(mode=0o700)
        self.record(self.root)
        self.helper_binds = []
        for name, capability in (('newuidmap', 'cap_setuid'), ('newgidmap', 'cap_setgid')):
            destination = self.root / name
            with destination.open('xb'):
                pass
            destination.chmod(0o755)
            self.record(destination)
            shutil.copyfile('/usr/bin/' + name, destination)
            command(['setcap', capability + '=ep', str(destination)])
            if command(['getcap', str(destination)]).strip() != str(destination) + ' ' + capability + '=ep':
                raise ValueError('Mapping helper capability does not match')
            self.helper_binds.append(str(destination) + ':/usr/bin/' + name)
        staging = self.root / 'rootfs'
        staging.mkdir(mode=0o755)
        # Public image paths must remain readable by the execution UID even
        # when the installed supervisor creates private files with umask 077.
        staging.chmod(0o755)
        self.record(staging)
        for name in ('usr', 'proc', 'sys', 'dev', 'work', 'run', 'tmp', 'etc', 'controller'):
            directory = staging / name
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
            if name in ('work', 'controller'):
                account = self.config['worker' if name == 'work' else 'controller']
                os.chown(directory, account['uid'], account['gid'])
                # Trusted setup has no DAC override. It must traverse /work
                # to write its own readiness file and bind the user runtime.
                # Search grants neither listing nor access to private job data.
                directory.chmod(0o701 if name == 'work' else 0o700)
            self.record(directory)
        for name in ('bin', 'sbin', 'lib', 'lib64'):
            path = staging / name
            path.symlink_to('usr/' + name)
            self.record(path, allow_link=True)
        extra = worker_files(self.target(), (self.installation / 'worker_launch.py').read_text(), storage_driver='overlay')
        extra['worker_launch.py'] = extra.pop('worker-launch.py')
        extra['etc/systemd/user/worker-probe.service'] = (
            '[Unit]\nDescription=Prepare the disposable Forgejo worker\n'
            '[Service]\nType=oneshot\nRemainAfterExit=yes\nDelegate=yes\n'
            'ExecStart=/usr/bin/python3 -B /worker_start.py\n'
            'StandardOutput=file:/work/worker-result.json\nStandardError=file:/work/probe-error.log\n')
        for name in ('worker_start.py', 'worker_probe.py', 'controller_launch.py', 'native_tools.py',
                     'host_network.py', 'host_egress.py', 'network_setup.py', 'gateway_launch.py'):
            extra[name] = (self.installation / name).read_text()
        extra['gateway-launch.py'] = extra.pop('gateway_launch.py')
        # Reuse the proved launcher with production egress setup in place of
        # the controlled peer setup used by the independent acceptance fixture.
        extra['network_setup.py'] = (self.installation / 'network_start.py').read_text().replace(
            'from network_setup import command',
            'from trusted_commands import command')
        extra['trusted_commands.py'] = (self.installation / 'network_setup.py').read_text()
        self.network = observe_configuration()
        configuration = json.loads(extra['worker.json'])
        configuration.update(native_network_helper=True, job_image_id=self.manifest['job_image_id'],
            network={'host_network_inode': os.stat('/proc/self/ns/net').st_ino,
                     'controller': self.config['controller'], 'egress': self.network})
        extra['worker.json'] = json.dumps(configuration)
        extra['etc/resolv.conf'] = 'nameserver ' + dns_forward(self.network) + '\noptions timeout:2 attempts:1\n'
        extra['etc/nsswitch.conf'] = extra['etc/nsswitch.conf'].replace('hosts: files\n', 'hosts: files dns\n')
        extra['etc/ssl/certs/ca-certificates.crt'] = Path('/etc/ssl/certs/ca-certificates.crt').read_text()
        extra['etc/containers/containers.conf'] += ('helper_binaries_dir=["/etc/runner-tools","/usr/lib/podman","/usr/libexec/podman"]\n'
                                                  '[network]\nfirewall_driver="nftables"\n')
        extra['etc/containers/policy.json'] = json.dumps(image_policy(self.manifest))
        extra['work/network-ready'] = ''
        extra['work/job-workspace/.owned'] = 'Disposable workspace\n'
        for relative, content in extra.items():
            destination = staging / relative
            missing = []
            parent = destination.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for parent in reversed(missing):
                parent.mkdir(mode=0o755)
                parent.chmod(0o755)
                if parent.is_relative_to(staging / 'work'):
                    os.chown(parent, self.config['worker']['uid'], self.config['worker']['gid'])
                    parent.chmod(0o700)
                self.record(parent)
            if isinstance(content, tuple):
                if content[0] != 'link':
                    raise ValueError('Unexpected trusted image declaration')
                destination.symlink_to(content[1])
                self.record(destination, allow_link=True)
            else:
                destination.write_text(content)
                destination.chmod(0o600 if relative == 'work/network-ready' else 0o444)
                self.record(destination)
        for name, relative, mode in (('job.oci', 'work/input/job.oci', 0o444),
                                     ('forgejo-runner', 'etc/runner-tools/forgejo-runner', 0o555),
                                     ('netavark', 'etc/runner-tools/netavark', 0o555)):
            destination = staging / relative
            if not destination.parent.exists():
                destination.parent.mkdir(mode=0o755)
                destination.parent.chmod(0o755)
                self.record(destination.parent)
            with destination.open('xb'):
                pass
            destination.chmod(mode)
            self.record(destination)
            shutil.copyfile(self.assets_root / name, destination)
            verify(destination, self.manifest['artifacts'][name])
        image = self.root / 'worker.ext4'
        with image.open('xb'):
            pass
        image.chmod(0o600)
        self.record(image)
        self.capacity()
        command(['fallocate', '-l', str(self.config['limits']['disk_bytes']), str(image)])
        command(['mkfs.ext4', '-q', '-F', '-m', '0', '-d', str(staging), str(image)])
        return self.allocation(invocation='0'*32)

    def allocation(self, *, invocation=None):
        metadata = (self.root / 'worker.ext4').lstat()
        return {'device': metadata.st_dev, 'inode': metadata.st_ino, 'uid': metadata.st_uid,
                'gid': metadata.st_gid, 'mode': metadata.st_mode, 'nlink': metadata.st_nlink,
                'invocation': invocation or self.unit.invocation}

    def run(self, registration):
        properties = unit_properties(self.config, self.root / 'worker.ext4', self.helper_binds)
        argv = ['systemd-run', '--quiet', '--unit=' + self.unit.name]
        argv += ['--property=' + setting for setting in properties]
        argv += ['/usr/bin/python3', '-B', '/worker_launch.py']
        self.unit.attempted = True
        command(argv)
        self.unit.claim()
        self.persist()
        checkpoint = self.store.read()
        checkpoint['allocation'] = self.allocation()
        self.store.write(checkpoint)
        observed = self.unit.observe()
        if observed.get('InvocationID') != self.unit.invocation:
            raise ValueError('Runtime invocation changed before transport startup')
        start_gateway(self.target(), observed, self.network, self.gateway)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            observed = self.unit.observe()
            if observed.get('InvocationID') != self.unit.invocation or observed.get('ActiveState') != 'active':
                raise ValueError('Runtime stopped during admission preparation')
            report = read_worker_result(self.target(), observed)
            if report['diagnostic']:
                raise RuntimeError('Owned worker startup failed')
            if report['observations'] is not None:
                if report['observations'] != {'ready': True}:
                    raise ValueError('Worker readiness did not match')
                break
            time.sleep(0.1)
        else:
            raise TimeoutError('Worker preparation deadline expired')
        request = {'url': 'https://forgejo.infra.supermorphic.com', 'label': self.config['label'],
                   'job_image': 'localhost/forgejo-job:approved', 'worker_uid': self.config['worker']['uid'],
                   'controller_uid': self.config['controller']['uid'], 'controller_gid': self.config['controller']['gid'],
                   'job_seconds': self.config['limits']['job_seconds'],
                   'uuid': registration.uuid, 'token': registration.token, 'handle': self.handle}
        self.run_controller(observed, request)

    def run_controller(self, observed, request):
        # Pin every namespace and the image root before creating the controller.
        process = os.open('/proc/' + observed['MainPID'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors = {}
        try:
            for name in ('net', 'mnt', 'pid'):
                descriptors[name] = os.open('ns/' + name, os.O_RDONLY, dir_fd=process)
            descriptors['root'] = os.open('root', os.O_RDONLY | os.O_DIRECTORY, dir_fd=process)
            if self.unit.observe().get('InvocationID') != self.unit.invocation:
                raise ValueError('Runtime changed before controller handoff')
            group = observed['ControlGroup']
            expected_group = '/system.slice/' + self.unit.name
            if group != expected_group:
                raise ValueError('Controller ancestor differs from its owned unit')
            controller_group = Path('/sys/fs/cgroup' + group) / 'controller'
            controller_group.mkdir(mode=0o700)
            if controller_group.lstat().st_uid != 0:
                raise ValueError('Controller cgroup is not authority-owned')
            descriptors['cgroup'] = os.open(controller_group / 'cgroup.procs', os.O_WRONLY | os.O_NOFOLLOW)
            def attach():
                os.write(descriptors['cgroup'], str(os.getpid()).encode())
                os.close(descriptors['cgroup'])
            args = ['/usr/bin/nsenter', '--mount=/proc/self/fd/' + str(descriptors['mnt']),
                    '--pid=/proc/self/fd/' + str(descriptors['pid']),
                    '--net=/proc/self/fd/' + str(descriptors['net']),
                    '--root=/proc/self/fd/' + str(descriptors['root']), '--wdns=/controller', '--',
                    '/usr/bin/python3', '-B', '/controller_launch.py']
            child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=tuple(descriptors.values()), preexec_fn=attach,
                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
            try:
                child.communicate(json.dumps(request).encode(), timeout=self.config['limits']['job_seconds'])
            except BaseException:
                # Outer teardown also terminates the entire bounded process tree.
                child.kill()
                child.wait(timeout=5)
                raise
            if child.returncode:
                raise RuntimeError('One-job controller did not complete')
        finally:
            for descriptor in descriptors.values():
                os.close(descriptor)
            os.close(process)

    def restore_receipt(self, generation):
        self.generation = generation
        path = self.state_root / 'resources.json'
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != 0
                    or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise ValueError('Ownership receipt is not private and authority-owned')
            raw = stream.read(131073)
            if len(raw) > 131072:
                raise ValueError('Ownership receipt exceeded its fixed bound')
        value = validate_receipt(self.config, generation, json.loads(raw))
        self.resources.entries = [(self.root / row['path'], tuple(row['identity']), tuple(row['parent_identity']))
                                  for row in value['resources']]
        self.unit = OwnedUnit(value['unit'], self.root / 'worker.ext4')
        self.unit.invocation = value['invocation']
        # An interrupted systemd-run can leave a unit without a response. Its
        # exact root-owned image and recorded generation still bind cleanup.
        self.unit.attempted = True

    def destroy(self, generation, expected):
        if not os.path.lexists(self.root) and not os.path.lexists(self.state_root / 'resources.json'):
            from verify import observe
            if not observe(self.config)['runtime_absent']:
                raise ValueError('Allocation disappeared while its processes remain')
            return
        self.restore_receipt(generation)
        surviving = remaining_resources(self.resources.entries)
        if expected is not None:
            recorded = next((owned for path, owned, _ in self.resources.entries
                             if path == self.root / 'worker.ext4'), None)
            expected_image = tuple(expected[key] for key in ('device', 'inode', 'uid', 'gid', 'mode', 'nlink'))
            if recorded != expected_image:
                raise ValueError('Allocation differs from its checkpoint')
            if self.unit.invocation is not None and expected['invocation'] not in ('0'*32, self.unit.invocation):
                raise ValueError('Allocation invocation differs from its checkpoint')
        if self.unit.cleanup():
            raise RuntimeError('Owned unit termination was not established')
        if self.gateway.get('helper') is not None:
            self.gateway['helper'].wait(timeout=5)
        if self.gateway.get('stderr') is not None:
            self.gateway['stderr'].close()
        for account in (self.target()['worker'], {**self.config['controller'],
                        'user': self.config['controller']['name'],
                        'subid_count': self.config['controller']['subuid_count']}):
            if worker_cleanup_errors({**self.target(), 'worker': account}):
                raise RuntimeError('Owned processes survived termination')
        deadline = time.monotonic() + 5
        while image_attached(self.root / 'worker.ext4'):
            if time.monotonic() >= deadline:
                raise RuntimeError('Worker image is still attached')
            time.sleep(0.1)
        # Recheck after process termination, immediately before deleting. A
        # previous interruption may already have removed part of this receipt.
        self.resources.entries = remaining_resources(surviving)
        if self.resources.cleanup():
            raise RuntimeError('Resource cleanup could not establish absence')
        # Only remove the private receipt after every recorded resource is gone.
        descriptor = os.open(self.state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            metadata = os.stat('resources.json', dir_fd=descriptor, follow_symlinks=False)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != 0
                    or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise ValueError('Receipt ownership changed during cleanup')
            os.unlink('resources.json', dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if hasattr(self, 'services') and not self.services <= active_services():
            raise RuntimeError('An existing active service changed during this generation')
