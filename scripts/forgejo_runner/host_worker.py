"""Independent host observations for the offline temporary rootless worker stage."""

import json
import os
from pathlib import Path
import re
import stat


def validate_worker_budget(target, capacity=None):
    limits = target['limits']
    ranges = {'memory_bytes': (512 * 1024**2, 1024**3),
              'disk_bytes': (256 * 1024**2, 1024**3),
              'pids': (128, 256), 'cpu_percent': (25, 50), 'job_seconds': (30, 120)}
    if any(type(limits[key]) is not int or not low <= limits[key] <= high
           for key, (low, high) in ranges.items()):
        raise ValueError('Worker probe exceeds its bounded experiment budgets')
    if capacity is not None:
        if (capacity['memory_available'] < limits['memory_bytes'] + 2 * 1024**3
                or capacity['memory_total'] < limits['memory_bytes'] * 4
                or capacity['disk_available'] < limits['disk_bytes'] + 2 * 1024**3):
            raise ValueError('Insufficient host reserve; no setup was performed')


def validate_job_budget(target, capacity=None):
    limits = target['limits']
    ranges = {'memory_bytes': (1024**3, 2 * 1024**3),
              'disk_bytes': (8 * 1024**3, 16 * 1024**3),
              'pids': (256, 512), 'cpu_percent': (25, 50), 'job_seconds': (300, 900)}
    if any(type(limits[key]) is not int or not low <= limits[key] <= high
           for key, (low, high) in ranges.items()):
        raise ValueError('Native job exceeds its bounded experiment budgets')
    if capacity is not None:
        # Reserve 4GiB for existing services; include the at-most-2GiB asset
        # staging copy as well as the finite worker image in the disk reserve.
        if (capacity['memory_available'] < limits['memory_bytes'] + 4 * 1024**3
                or capacity['memory_total'] < limits['memory_bytes'] + 4 * 1024**3
                or capacity['disk_available'] < limits['disk_bytes'] + 4 * 1024**3):
            raise ValueError('Insufficient native job reserve; no setup was performed')


def validate_workload_budget(target, capacity=None):
    limits = target['limits']
    ranges = {'memory_bytes': (1024**3, 3 * 1024**3),
              'disk_bytes': (16 * 1024**3, 32 * 1024**3),
              'pids': (512, 1024), 'cpu_percent': (50, 200), 'job_seconds': (900, 10800)}
    if any(type(limits[key]) is not int or not low <= limits[key] <= high
           for key, (low, high) in ranges.items()):
        raise ValueError('Native workload exceeds its finite experiment budgets')
    if capacity is not None:
        if (capacity['memory_available'] < limits['memory_bytes'] + 4 * 1024**3
                or capacity['memory_total'] < limits['memory_bytes'] + 4 * 1024**3
                or capacity['disk_available'] < limits['disk_bytes'] + 4 * 1024**3
                or type(capacity.get('cpu_count')) is not int
                or limits['cpu_percent'] > capacity['cpu_count'] // 2 * 100):
            raise ValueError('Insufficient native workload reserve; no setup was performed')


def worker_cleanup_errors(target, proc=Path('/proc')):
    worker = target['worker']
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            status = dict(line.split(':', 1) for line in (process / 'status').read_text().splitlines() if ':' in line)
            identities = list(map(int, status['Uid'].split()))
        except FileNotFoundError:
            continue
        if any(uid == worker['uid'] or worker['subuid_start'] <= uid < worker['subuid_start'] + worker['subid_count']
               for uid in identities):
            return ['Worker processes survived; allocation preserved']
    return []


def validate_process_boundary(target, records, outer, host_net, host_userns, host_pidns):
    """Host-kernel metadata, not a container's assertion about its own ancestry."""
    worker = target['worker']
    if not records or not outer.startswith('/system.slice/') or '..' in outer.split('/'):
        raise ValueError('Worker process observation is incomplete')
    direct = subordinate = False
    for process in records:
        uid, gid = process['uid'], process['gid']
        direct |= uid == worker['uid']
        subordinate |= worker['subuid_start'] <= uid < worker['subuid_start'] + worker['subid_count']
        uid_allowed = uid == worker['uid'] or worker['subuid_start'] <= uid < worker['subuid_start'] + worker['subid_count']
        gid_allowed = gid == worker['gid'] or worker['subgid_start'] <= gid < worker['subgid_start'] + worker['subid_count']
        if (not uid_allowed or not gid_allowed
                or not process['cgroup'].startswith(outer + '/')
                or process['net'] == host_net
                or process['pidns'] == host_pidns
                or process['userns'] == host_userns and process['effective_caps'] != 0):
            raise ValueError('Worker process escaped its declared boundary')
    if not direct or not subordinate:
        raise ValueError('Worker and subordinate container observations are required')


def worker_files(target, launcher, *, oci_assets=(), registry_images=(), storage_driver='vfs'):
    """Private image NSS records authorize only the declared unused ID ranges."""
    if storage_driver not in ('vfs', 'overlay'):
        raise ValueError('Unknown owned worker storage driver')
    worker = target['worker']
    user, uid, gid = worker['user'], worker['uid'], worker['gid']
    if (len(oci_assets) > 5 or len(set(oci_assets)) != len(oci_assets)
            or any(re.fullmatch(r'image-[0-4]\.oci', name) is None for name in oci_assets)):
        raise ValueError('Invalid declared OCI asset paths')
    files = {
        'worker-launch.py': launcher,
        'worker.json': json.dumps({'worker': worker, 'limits': target['limits'], 'storage_driver':storage_driver}),
        'etc/passwd': f'root:x:0:0:root:/root:/usr/sbin/nologin\n{user}:x:{uid}:{gid}:fixture:/work:/usr/sbin/nologin\n',
        'etc/group': f'root:x:0:\n{user}:x:{gid}:\n',
        'etc/nsswitch.conf': 'passwd: files\ngroup: files\nshadow: files\nhosts: files\n',
        'etc/hosts': '127.0.0.1 localhost\n::1 localhost\n',
        'etc/resolv.conf': '# Offline fixture: no external resolver.\n',
        'etc/subuid': f'{user}:{worker["subuid_start"]}:{worker["subid_count"]}\n',
        'etc/subgid': f'{user}:{worker["subgid_start"]}:{worker["subid_count"]}\n',
        'etc/machine-id': '11111111111111111111111111111111\n',
        # The finite root-owned ancestor bounds the whole worker. systemd's
        # percentage default would give each child a fraction of that already
        # bounded maximum, preventing ordinary Podman startup.
        'etc/systemd/user.conf': '[Manager]\nDefaultTasksMax=infinity\n'
            'DefaultEnvironment=CONTAINERS_STORAGE_CONF=/etc/containers/storage.conf\n',
        'etc/containers/storage.conf': f'[storage]\ndriver="{storage_driver}"\ngraphroot="/work/graph"\nrootless_storage_path="/work/graph"\nrunroot="/run/user/{uid}/containers"\n',
        # Preserve a result-inspection grace period without retaining hundreds
        # of completed Ansible exec monitors for Podman's default five minutes.
        'etc/containers/containers.conf': '[engine]\ncgroup_manager="systemd"\nevents_logger="file"\n'
            'image_copy_tmp_dir="/work/image-tmp"\nexit_command_delay=30\n',
        'etc/containers/policy.json': '{"default":[{"type":"reject"}],"transports":'
            '{"tarball":{"": [{"type":"insecureAcceptAnything"}]}}}\n',
        'etc/systemd/user/worker-probe.service':
            '[Unit]\nDescription=Offline owned worker feasibility probe\n'
            '[Service]\nType=oneshot\nRemainAfterExit=yes\nDelegate=yes\n'
            'ExecStart=/usr/bin/python3 /probe.py\nStandardOutput=file:/work/worker-result.json\n'
            'StandardError=file:/work/probe-error.log\n',
        'etc/systemd/user/default.target.wants/worker-probe.service': ('link', '../worker-probe.service'),
        # Ensure the runtime directory exists in the bounded image.
        'work/run/.fixture': 'synthetic runtime allocation\n',
        'work/image-tmp/.fixture': 'synthetic image copy allocation\n',
        # OCI archive format detection uses this standard prefix independently
        # of engine copy settings. It resolves only to the finite private image.
        'var/tmp': ('link', '../work/image-tmp'),
    }
    if storage_driver == 'overlay':
        files['etc/containers/storage.conf'] += '[storage.options.overlay]\nmount_program=""\n'
    if oci_assets:
        policy = json.loads(files['etc/containers/policy.json'])
        policy['transports']['oci-archive'] = {
            '/work/input/' + name: [{'type': 'insecureAcceptAnything'}] for name in oci_assets}
        files['etc/containers/policy.json'] = json.dumps(policy)
    if registry_images:
        from scripts.forgejo_runner.workload import registry_sources, SCENARIOS
        approved = registry_sources(suite='stock-runtime')
        for selector in SCENARIOS:
            approved |= registry_sources(selector)
        if any(not isinstance(ref, str) or ref not in approved for ref in registry_images):
            raise ValueError('Invalid workload registry source')
        def policy_identity(reference):
            # The Docker transport matches a tag or a digest, never both.
            # Approval above still checks the original exact configured source.
            if '@' not in reference:
                return reference
            name, digest = reference.split('@', 1)
            namespace, image = name.rsplit('/', 1)
            return namespace + '/' + image.split(':', 1)[0] + '@' + digest
        policy = json.loads(files['etc/containers/policy.json'])
        policy['transports']['docker'] = {
            policy_identity(ref): [{'type': 'insecureAcceptAnything'}]
            for ref in registry_images}
        files['etc/containers/policy.json'] = json.dumps(policy)
    return files


def validate_runtime_mount(target, source, runtime, work_device, filesystem):
    worker = target['worker']
    if ((source.st_dev, source.st_ino) != (runtime.st_dev, runtime.st_ino)
            or runtime.st_dev != work_device
            or runtime.st_uid != worker['uid'] or runtime.st_gid != worker['gid']
            or not stat.S_ISDIR(runtime.st_mode) or stat.S_IMODE(runtime.st_mode) != 0o700
            or filesystem.f_blocks * filesystem.f_frsize > target['limits']['disk_bytes']):
        raise ValueError('Worker runtime directory is outside its owned bounded storage')


def validate_runtime_storage(target, root, work):
    """Read mount identity through pinned process and directory descriptors."""
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        source = os.open('run', flags, dir_fd=work)
        descriptors.append(source)
        parent = root
        for name in ('run', 'user', str(target['worker']['uid'])):
            parent = os.open(name, flags, dir_fd=parent)
            descriptors.append(parent)
        validate_runtime_mount(target, os.fstat(source), os.fstat(parent),
                               os.fstat(work).st_dev, os.fstatvfs(parent))
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_worker_result(target, unit, proc=Path('/proc')):
    """Pin the manager's proc directory; never follow paths provided by a worker."""
    pid = unit.get('MainPID', '0')
    empty = {'observations': None, 'diagnostic': ''}
    if not pid.isdigit() or pid == '0':
        return empty
    descriptors = []
    try:
        process = os.open(proc / pid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(process)
        descriptor = os.open('cgroup', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=process)
        with os.fdopen(descriptor) as stream:
            group = stream.read(4096).strip().removeprefix('0::')
        outer = unit['ControlGroup']
        if not outer.startswith('/system.slice/') or not (group == outer or group.startswith(outer + '/')):
            raise ValueError('Result producer is outside the owned ancestor')
        # This is a kernel procfs link belonging to the pinned live process,
        # not a user-controlled symlink in the disposable filesystem.
        root = os.open('root', os.O_RDONLY | os.O_DIRECTORY, dir_fd=process)
        descriptors.append(root)
        work = os.open('work', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        descriptors.append(work)
        contents = {}
        for name, limit in (('worker-result.json', 16384), ('probe-error.log', 4096),
                            ('api-error.log', 4096), ('native-error.log', 4096),
                            ('native-job-error.log', 4096)):
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=work)
            except FileNotFoundError:
                contents[name] = ''
                continue
            with os.fdopen(descriptor) as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                        or metadata.st_uid != target['worker']['uid']):
                    raise ValueError('Unsafe worker result file')
                if name == 'worker-result.json' and metadata.st_size > limit:
                    raise ValueError('Worker result exceeded its bound')
                contents[name] = stream.read(limit)
        try:
            observation = json.loads(contents['worker-result.json']) if contents['worker-result.json'] else None
        except ValueError:
            observation = None  # A bounded single write is still in progress.
        if observation is not None:
            validate_runtime_storage(target, root, work)
        diagnostic = contents['probe-error.log']
        if diagnostic:
            diagnostic = (contents['native-job-error.log'] + '\n' + contents['native-error.log']
                          + '\n' + contents['api-error.log'] + '\n' + diagnostic)[:4096]
        return {'observations': observation, 'diagnostic': diagnostic}
    except FileNotFoundError:
        return empty
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def observe_worker_boundary(target, observed, unit):
    """Read kernel process ancestry and effective ancestor limits from the host."""
    required = {'rootless_api', 'sibling_containers', 'mapped_bind', 'private_loopback',
                'published_loopback', 'user_manager', 'bounded_storage'}
    if not isinstance(observed, dict) or set(observed) != required or any(observed[key] is not True for key in required):
        raise ValueError('Worker probe lacks required independent workload outcomes')
    outer = unit.get('ControlGroup', '')
    if not outer.startswith('/system.slice/') or '..' in outer.split('/'):
        raise ValueError('Worker ancestor cgroup is missing')
    path = Path('/sys/fs/cgroup' + outer)
    limits = target['limits']
    expectations = {'memory.max': str(limits['memory_bytes']), 'memory.swap.max': '0',
                    'pids.max': str(limits['pids'])}
    for name, expected in expectations.items():
        control = path / name
        if control.stat().st_uid != 0 or control.read_text().strip() != expected:
            raise ValueError('Worker ancestor limit is absent or replaceable')
    cpu = path / 'cpu.max'
    quota, period = cpu.read_text().split()
    if cpu.stat().st_uid != 0 or quota == 'max' or int(quota) * 100 != int(period) * limits['cpu_percent']:
        raise ValueError('Worker ancestor CPU limit is absent or replaceable')
    worker = target['worker']
    records = []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            status = dict(line.split(':', 1) for line in (process / 'status').read_text().splitlines() if ':' in line)
            uid = int(status['Uid'].split()[1])
            if uid != worker['uid'] and not worker['subuid_start'] <= uid < worker['subuid_start'] + worker['subid_count']:
                continue
            records.append({'uid': uid, 'gid': int(status['Gid'].split()[1]),
                'cgroup': (process / 'cgroup').read_text().strip().removeprefix('0::'),
                'net': (process / 'ns/net').stat().st_ino, 'userns': (process / 'ns/user').stat().st_ino,
                'pidns': (process / 'ns/pid').stat().st_ino,
                'effective_caps': int(status['CapEff'], 16)})
        except FileNotFoundError:
            continue  # A short-lived helper ended during metadata observation.
    validate_process_boundary(target, records, outer, os.stat('/proc/self/ns/net').st_ino,
                              os.stat('/proc/self/ns/user').st_ino, os.stat('/proc/self/ns/pid').st_ino)
    return {**observed, 'outer_limits': True, 'process_boundary': True}


def observe_job_boundary(target, observed, unit):
    required = {'one_job', 'job_runtime'}
    if isinstance(observed, dict) and 'job_workload' in observed:
        required.add('job_workload')
    if isinstance(observed, dict) and 'job_public_access' in observed:
        required.add('job_public_access')
    if not isinstance(observed, dict) or any(observed.get(key) is not True for key in required):
        raise ValueError('Native job lacks required workload outcomes')
    result = observe_worker_boundary(target, {k: v for k, v in observed.items() if k not in required}, unit)
    return {**result, **{name: True for name in required}}
