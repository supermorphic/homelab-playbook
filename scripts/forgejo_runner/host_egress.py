"""Trusted public transport for an owned worker namespace, never host routing."""

import ipaddress
import json
import os
from pathlib import Path
import subprocess
import stat
import time

FORGEJO_HOST = 'forgejo.infra.supermorphic.com'
EGRESS_DENIED = (('10.57.0.2', 443), ('fd57::2', 443),
                 ('192.0.2.20', 443), ('2001:db8:57::20', 443))
NONPUBLIC4 = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
              '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
              '192.88.99.0/24', '192.168.0.0/16', '198.18.0.0/15',
              '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4')
NONPUBLIC6 = ('::/128', '::1/128', '::ffff:0:0/96', '64:ff9b::/96', '64:ff9b:1::/48',
              '100::/64', '2001::/23', '2001:db8::/32', '2002::/16', '3ffe::/16',
              '3fff::/20', '5f00::/16', 'fc00::/7', 'fe80::/10', 'ff00::/8')


def address(value):
    if not isinstance(value, str) or '%' in value:
        raise ValueError('Invalid trusted network address')
    result = ipaddress.ip_address(value)
    if result.version == 6 and result.ipv4_mapped is not None:
        raise ValueError('Mapped addresses cannot authorize network exceptions')
    return result


def validate_configuration(configuration):
    try:
        if set(configuration) != {'forgejo_addresses', 'host_addresses', 'dns_address'}:
            raise ValueError
        for name, maximum in (('forgejo_addresses', 16), ('host_addresses', 128)):
            values = configuration[name]
            if not isinstance(values, list) or not 1 <= len(values) <= maximum:
                raise ValueError
            normalized = [str(address(value)) for value in values]
            if normalized != values or len(set(normalized)) != len(normalized):
                raise ValueError
            if name == 'forgejo_addresses' and any(
                    value.is_loopback or value.is_link_local or value.is_multicast or value.is_unspecified
                    for value in map(address, values)):
                raise ValueError
        resolver = address(configuration['dns_address'])
        if resolver.is_multicast or resolver.is_unspecified or resolver.is_link_local or resolver.is_loopback:
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Invalid trusted public-egress configuration') from error
    return configuration


def resolver_address(source):
    if not isinstance(source, str) or len(source) > 65536:
        raise ValueError('Resolver observation exceeded its bound')
    for line in source.splitlines():
        fields = line.split('#', 1)[0].split()
        if fields and fields[0] == 'nameserver':
            if len(fields) != 2:
                raise ValueError('Malformed host resolver observation')
            resolver = address(fields[1])
            if resolver.is_unspecified or resolver.is_multicast or resolver.is_link_local:
                raise ValueError('Unsupported host resolver observation')
            return str(resolver)
    raise ValueError('An explicit host nameserver is required')


def validate_gateway(record, controller, outer, expected):
    try:
        if (record['uid'] != controller['uid'] or record['gid'] != controller['gid']
                or record['cgroup'] != outer and not record['cgroup'].startswith(outer + '/')
                or any(record[key] != expected[key] for key in
                       ('net', 'pidns', 'mnt', 'root_device', 'root_inode'))
                or any(record[key] != 0 for key in ('effective_caps', 'permitted_caps',
                                                   'bounding_caps', 'inheritable_caps', 'ambient_caps'))):
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Public gateway is outside its owned privilege or runtime boundary') from error


def dns_forward(configuration):
    return configuration['dns_address']


def transport_arguments(configuration, tap_descriptor):
    validate_configuration(configuration)
    if type(tap_descriptor) is not int or tap_descriptor < 3:
        raise ValueError('A preopened private TAP descriptor is required')
    return ['/usr/bin/slirp4netns', '--netns-type=tapfd', '--cidr=10.57.1.0/24',
            '--enable-ipv6', '--disable-host-loopback', '--disable-dns',
            '--enable-seccomp', str(tap_descriptor)]


def public_policy(configuration):
    """Exact exceptions precede public web access; all other traffic is dropped."""
    if __package__:
        from .host_network import policy_source
    else:
        from host_network import policy_source
    validate_configuration(configuration)
    rules = []
    resolver = dns_forward(configuration)
    family = 'ip' if address(resolver).version == 4 else 'ip6'
    for protocol in ('udp', 'tcp'):
        rules.append(f'  oifname "uplink0" {family} daddr {resolver} {protocol} dport 53 accept')
    for value in configuration['forgejo_addresses']:
        family = 'ip' if address(value).version == 4 else 'ip6'
        rules.append(f'  oifname "uplink0" {family} daddr {value} tcp dport 443 accept')
    for family, version, prohibited in (('ip', 4, NONPUBLIC4), ('ip6', 6, NONPUBLIC6)):
        blocked = list(prohibited) + [value for value in configuration['host_addresses']
                                      if address(value).version == version]
        networks = ipaddress.collapse_addresses(ipaddress.ip_network(value) for value in blocked)
        counter = 'denied4' if version == 4 else 'denied6'
        rules.append(f'  {family} daddr {{ ' + ', '.join(map(str, networks))
                     + f' }} counter name {counter} drop')
    rules += ['  ip daddr 0.0.0.0/0 tcp dport { 80, 443 } accept',
              '  ip6 daddr 2000::/3 tcp dport { 80, 443 } accept']
    for value, port in EGRESS_DENIED:
        family, counter = ('ip6', 'denied6') if ':' in value else ('ip', 'denied4')
        rules.append(f'  {family} daddr {value} tcp dport {port} counter name {counter} drop')
    source = policy_source()
    ending = '\n }\n}\n'
    if not source.endswith(ending):
        raise ValueError('Controlled policy structure is invalid')
    return source[:-len(ending)] + '\n' + '\n'.join(rules) + ending


def observe_configuration():
    resolver = resolver_address(Path('/etc/resolv.conf').read_text())
    lookup = subprocess.run(['/usr/bin/getent', 'ahosts', FORGEJO_HOST],
                            capture_output=True, text=True, check=True, timeout=10)
    if len(lookup.stdout) > 65536:
        raise ValueError('Forgejo address observation exceeded its bound')
    forgejo = sorted({str(address(line.split()[0])) for line in lookup.stdout.splitlines()})
    result = subprocess.run(['/usr/sbin/ip', '--json', 'address', 'show'],
                            capture_output=True, text=True, check=True, timeout=10)
    if len(result.stdout) > 65536:
        raise ValueError('Host address observation exceeded its bound')
    hosts = sorted({str(address(item['local'])) for interface in json.loads(result.stdout)
                    for item in interface['addr_info']})
    return validate_configuration({'forgejo_addresses': forgejo,
                                   'host_addresses': hosts, 'dns_address': resolver})


def process_record(process):
    status_fd = os.open('status', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=process)
    with os.fdopen(status_fd) as stream:
        status = dict(line.split(':', 1) for line in stream.read(65537).splitlines() if ':' in line)
    if (len(set(status['Uid'].split())) != 1 or len(set(status['Gid'].split())) != 1
            or status['Groups'].strip()):
        raise ValueError('Gateway identity retained additional authority')
    cgroup_fd = os.open('cgroup', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=process)
    with os.fdopen(cgroup_fd) as stream:
        cgroup = stream.read(4097).strip().removeprefix('0::')
    root = os.stat('root', dir_fd=process)
    return {'uid': int(status['Uid'].split()[1]), 'gid': int(status['Gid'].split()[1]),
            'net': os.stat('ns/net', dir_fd=process).st_ino,
            'mnt': os.stat('ns/mnt', dir_fd=process).st_ino,
            'pidns': os.stat('ns/pid', dir_fd=process).st_ino,
            'root_device': root.st_dev, 'root_inode': root.st_ino, 'cgroup': cgroup,
            **{name: int(status[key], 16) for name, key in (
                ('effective_caps', 'CapEff'), ('permitted_caps', 'CapPrm'),
                ('bounding_caps', 'CapBnd'), ('inheritable_caps', 'CapInh'), ('ambient_caps', 'CapAmb'))}}


def gateway_records(target, unit, expected, argv):
    records = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        process = None
        try:
            process = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            status_fd = os.open('status', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=process)
            with os.fdopen(status_fd) as stream:
                status = dict(line.split(':', 1) for line in stream.read(65537).splitlines() if ':' in line)
            if int(status['Uid'].split()[1]) != target['controller']['uid']:
                continue
            descriptor = os.open('cmdline', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=process)
            with os.fdopen(descriptor, 'rb') as stream:
                command = stream.read(16385).rstrip(b'\0').decode().split('\0')
            if command != argv:
                continue
            record = process_record(process)
            validate_gateway(record, target['controller'], unit['ControlGroup'], expected)
            records.append((int(path.name), record))
        except FileNotFoundError:
            continue
        finally:
            if process is not None:
                os.close(process)
    if len(records) != 1:
        raise ValueError('Exactly one owned unprivileged TAP transport is required')
    return records


def gateway_error_stream(root_descriptor):
    descriptor = os.open('work/gateway-error.log',
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_APPEND, 0o600, dir_fd=root_descriptor)
    return os.fdopen(descriptor, 'r+b', buffering=0)


def wait_network_ready(root_descriptor, *, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open('work/network-ready', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_descriptor)
            with os.fdopen(descriptor) as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                        or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022):
                    raise ValueError('Trusted network marker ownership changed')
                value = stream.read(64)
            if value == 'controlled-network-ready\n':
                return True
            if value:
                raise ValueError('Unexpected trusted network readiness marker')
        except FileNotFoundError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('Trusted network startup exceeded its bound')
        time.sleep(min(0.1, remaining))


def start_gateway(target, unit, configuration, state):
    """Pin owned kernel objects; join their storage/PID boundary before transport."""
    if not unit['ControlGroup'].startswith('/system.slice/forgejo-worker-'):
        raise ValueError('Gateway ancestor is outside the declared worker')
    process = os.open('/proc/' + unit['MainPID'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptors = {}
    try:
        for name in ('net', 'mnt', 'pid'):
            descriptors[name] = os.open('ns/' + name, os.O_RDONLY, dir_fd=process)
        descriptors['root'] = os.open('root', os.O_RDONLY | os.O_DIRECTORY, dir_fd=process)
        # A process in the ancestor would prevent cgroup v2 controller
        # delegation to sibling containers (the no-internal-process rule).
        transport = Path('/sys/fs/cgroup' + unit['ControlGroup']) / 'public-transport'
        transport.mkdir(mode=0o700)
        metadata = transport.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0:
            raise ValueError('Public transport cgroup is not owned by trusted setup')
        cgroup = os.open(str(transport / 'cgroup.procs'), os.O_WRONLY | os.O_NOFOLLOW)
        descriptors['cgroup'] = cgroup
        descriptors['host'] = os.open('/proc/self/ns/net', os.O_RDONLY)
        host_net = os.fstat(descriptors['host']).st_ino
        if os.fstat(descriptors['net']).st_ino == host_net:
            raise ValueError('Gateway target network is not private')
        expected = {'net': host_net, 'pidns': os.fstat(descriptors['pid']).st_ino,
                    'mnt': os.fstat(descriptors['mnt']).st_ino,
                    'root_device': os.fstat(descriptors['root']).st_dev,
                    'root_inode': os.fstat(descriptors['root']).st_ino}
        wait_network_ready(descriptors['root'])
        argv = transport_arguments(configuration, 9)
        state.update(expected=expected, argv=argv, stderr=gateway_error_stream(descriptors['root']))
        command = ['/usr/bin/nsenter', '--mount=/proc/self/fd/' + str(descriptors['mnt']),
                   '--pid=/proc/self/fd/' + str(descriptors['pid']),
                   '--root=/proc/self/fd/' + str(descriptors['root']), '--wdns=/work', '--',
                   '/usr/bin/python3', '/gateway-launch.py', str(descriptors['net']), str(descriptors['host']),
                   json.dumps(configuration), str(target['controller']['uid']) + ':' + str(target['controller']['gid'])]
        def attach():
            os.write(cgroup, str(os.getpid()).encode())
            os.close(cgroup)
        helper = subprocess.Popen(command, pass_fds=tuple(descriptors.values()), preexec_fn=attach,
                                  stdout=subprocess.DEVNULL, stderr=state['stderr'],
                                  env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        state['helper'] = helper
        deadline = time.monotonic() + 10
        while True:
            if helper.poll() is not None:
                raise ValueError('Trusted public transport failed during startup')
            try:
                gateway_records(target, unit, expected, argv)
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        ready = os.open('work/egress-ready', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o444, dir_fd=descriptors['root'])
        with os.fdopen(ready, 'w') as stream:
            stream.write('public-gateway-ready\n')
        return state
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)
        os.close(process)


def observe_public_boundary(target, observed, unit, gateway, observe_worker):
    if __package__:
        from .host_network import observe_network_boundary
    else:
        # The registered remote payload defines the trusted host observers in
        # one namespace; image-local imports are unnecessary on that side.
        observe_network_boundary = globals()['observe_network_boundary']
    required = {'dns', 'forgejo_https', 'public_http', 'public_https'}
    if not isinstance(observed, dict) or any(observed.get(name) is not True for name in required):
        raise ValueError('Public access evidence is incomplete')
    helper, expected, argv = (gateway[name] for name in ('helper', 'expected', 'argv'))
    if helper.poll() is not None:
        raise ValueError('Public transport terminated before independent verification')
    records = gateway_records(target, unit, expected, argv)
    result = observe_network_boundary(target, {k:v for k,v in observed.items() if k not in required},
        unit, observe_worker, excluded_pids=(records[0][0],), additional_denied=EGRESS_DENIED)
    return {**result, **{name: True for name in required}, 'gateway_boundary': True}
