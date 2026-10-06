"""Controlled, namespace-local network policy and independent kernel evidence."""

import errno
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess

ALLOWED = (('192.0.2.20', 8100), ('2001:db8:57::20', 8100))
DENIED = (('10.57.0.2', 8100), ('fd57::2', 8100),
          ('192.0.2.20', 8101), ('2001:db8:57::20', 8101))


def validate_network_budget(target, capacity=None):
    limits = target['limits']
    bounds = {'memory_bytes': (512 * 1024**2, 1024**3),
              'disk_bytes': (256 * 1024**2, 1024**3),
              'pids': (128, 256), 'cpu_percent': (25, 50), 'job_seconds': (30, 120)}
    if any(type(limits[key]) is not int or not low <= limits[key] <= high
           for key, (low, high) in bounds.items()):
        raise ValueError('Controlled network probe exceeds its bounded budgets')
    if capacity is not None and (
            capacity['memory_available'] < limits['memory_bytes'] + 4 * 1024**3
            or capacity['memory_total'] < limits['memory_bytes'] + 4 * 1024**3
            or capacity['disk_available'] < limits['disk_bytes'] + 4 * 1024**3):
        raise ValueError('Insufficient controlled network service reserve; no setup was performed')


def validate_network_target(target):
    """Only fixed synthetic peers, never arbitrary existing network services."""
    try:
        for name, expected in (('allowed_endpoints', ALLOWED), ('denied_endpoints', DENIED)):
            records = target[name]
            endpoints = {(str(ipaddress.ip_address(item['address'])), item['port']) for item in records}
            if len(records) != len(expected) or endpoints != set(expected):
                raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Network probe requires its complete controlled dual-stack endpoint set') from error


def check_connections(allowed, denied):
    for endpoint in allowed:
        with socket.create_connection((endpoint['address'], endpoint['port']), timeout=1) as connection:
            body = b''
            while not body.endswith(b'\n') and len(body) < 64:
                data = connection.recv(64 - len(body))
                if not data:
                    break
                body += data
            if body != b'owned-network-endpoint\n':
                raise ValueError('Allowed endpoint is not the controlled peer')
    for endpoint in denied:
        try:
            with socket.create_connection((endpoint['address'], endpoint['port']), timeout=1):
                raise ValueError('Denied controlled endpoint was reachable')
        except OSError as error:
            if not isinstance(error, TimeoutError) and error.errno not in (errno.EACCES, errno.EPERM):
                # Refusal, missing routes or a dead peer do not prove filtering.
                raise


def policy_source():
    """Public fixture addresses only; default denial applies to both families."""
    return '''table inet forgejo_fixture {
 counter denied4 {}
 counter denied6 {}
 chain output {
  type filter hook output priority 0; policy drop;
  ct state established,related accept
  oifname "lo" ip daddr 127.0.0.0/8 accept
  oifname "lo" ip6 daddr ::1 accept
  meta l4proto ipv6-icmp icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert } accept
  ip daddr 192.0.2.20 tcp dport 8100 accept
  ip6 daddr 2001:db8:57::20 tcp dport 8100 accept
  ip daddr 10.57.0.2 tcp dport 8100 counter name denied4 drop
  ip daddr 192.0.2.20 tcp dport 8101 counter name denied4 drop
  ip6 daddr fd57::2 tcp dport 8100 counter name denied6 drop
  ip6 daddr 2001:db8:57::20 tcp dport 8101 counter name denied6 drop
 }
}
'''


def validate_policy_observation(policy):
    try:
        entries = policy['nftables']
        chains = [item['chain'] for item in entries if 'chain' in item]
        if (len(chains) != 1 or chains[0]['family'] != 'inet'
                or chains[0]['table'] != 'forgejo_fixture' or chains[0]['name'] != 'output'
                or chains[0]['hook'] != 'output' or chains[0]['policy'] != 'drop'):
            raise ValueError
        counters = [item['counter'] for item in entries if 'counter' in item]
        if len(counters) != 2 or {item['name'] for item in counters} != {'denied4', 'denied6'}:
            raise ValueError
        if any(item['family'] != 'inet' or item['table'] != 'forgejo_fixture'
               or type(item['packets']) is not int or item['packets'] < 6 for item in counters):
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Kernel policy and dual-stack denial counters are incomplete') from error


def validate_peer_records(target, records, outer, host_net, worker_net, worker_pidns):
    if len(records) != 1:
        raise ValueError('Exactly one controlled peer process is required')
    peer = records[0]
    controller = target['controller']
    if (peer['uid'] != controller['uid'] or peer['gid'] != controller['gid']
            or peer['net'] in (host_net, worker_net) or peer['pidns'] != worker_pidns
            or peer['cgroup'] != outer and not peer['cgroup'].startswith(outer + '/')
            or any(peer[key] != 0 for key in ('effective_caps', 'permitted_caps', 'bounding_caps',
                                             'inheritable_caps', 'ambient_caps'))):
        raise ValueError('Controlled peer privilege or namespace boundary failed')


def listening_endpoints(output):
    result = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] != 'LISTEN':
            raise ValueError('Malformed kernel listener observation')
        address, port = fields[3].rsplit(':', 1)
        result.add((str(ipaddress.ip_address(address.strip('[]'))), int(port)))
    return result


def namespace_command(process, argv):
    """Use a pinned kernel namespace descriptor, not a reusable process pathname."""
    descriptor = os.open('ns/net', os.O_RDONLY, dir_fd=process)
    try:
        result = subprocess.run(['/usr/bin/nsenter', '--net=/proc/self/fd/' + str(descriptor), '--', *argv],
            pass_fds=(descriptor,), capture_output=True, text=True, timeout=10,
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}, check=False)
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError('Bounded kernel namespace observation failed')
        return result.stdout
    finally:
        os.close(descriptor)


def observe_network_boundary(target, observed, unit, observe_worker):
    required = {'allowed_ipv4', 'allowed_ipv6', 'denied_ipv4', 'denied_ipv6',
                'policy_immutable', 'alternate_network_modes'}
    if not isinstance(observed, dict) or any(observed.get(name) is not True for name in required):
        raise ValueError('Controlled network probe lacks required workload outcomes')
    result = observe_worker(target, {name: value for name, value in observed.items() if name not in required}, unit)
    outer = unit['ControlGroup']
    process = os.open('/proc/' + unit['MainPID'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        worker_net = os.stat('ns/net', dir_fd=process).st_ino
        worker_pidns = os.stat('ns/pid', dir_fd=process).st_ino
        host_net = os.stat('/proc/self/ns/net').st_ino
        if worker_net == host_net:
            raise ValueError('Policy namespace is not private')
        validate_policy_observation(json.loads(namespace_command(process,
            ['/usr/sbin/nft', '--json', 'list', 'table', 'inet', 'forgejo_fixture'])))
        peers = []
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                status = dict(line.split(':', 1) for line in (path / 'status').read_text().splitlines() if ':' in line)
                if int(status['Uid'].split()[1]) != target['controller']['uid']:
                    continue
                peers.append({'pid': path.name, 'uid': int(status['Uid'].split()[1]),
                    'gid': int(status['Gid'].split()[1]),
                    'net': (path / 'ns/net').stat().st_ino, 'pidns': (path / 'ns/pid').stat().st_ino,
                    'cgroup': (path / 'cgroup').read_text().strip().removeprefix('0::'),
                    **{name: int(status[key], 16) for name, key in (
                        ('effective_caps', 'CapEff'), ('permitted_caps', 'CapPrm'),
                        ('bounding_caps', 'CapBnd'), ('inheritable_caps', 'CapInh'), ('ambient_caps', 'CapAmb'))}})
            except FileNotFoundError:
                continue
        validate_peer_records(target, peers, outer, host_net, worker_net, worker_pidns)
        peer = os.open('/proc/' + peers[0]['pid'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.stat('ns/net', dir_fd=peer).st_ino != peers[0]['net']:
                raise ValueError('Peer identity changed')
            if not set(ALLOWED + DENIED) <= listening_endpoints(namespace_command(peer, ['/usr/bin/ss', '-H', '-ltn'])):
                raise ValueError('Controlled allowed and denied listeners are not independently alive')
        finally:
            os.close(peer)
    finally:
        os.close(process)
    return {**result, **{name: True for name in required}, 'kernel_policy': True, 'controlled_peer': True}
