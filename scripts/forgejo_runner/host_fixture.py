"""Explicit disposable Debian target validation and observational SSH preflight."""

import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_COMMANDS = (
    "podman", "systemctl", "systemd-run", "ip", "nft", "mount", "umount",
    "newuidmap", "newgidmap",
)


def positive(value):
    return type(value) is int and value > 0


def overlaps(start, count, other_start, other_count):
    return start < other_start + other_count and other_start < start + count


def validate_target(target: dict) -> dict:
    """Reject undeclared authority and unsafe allocations before contacting SSH."""
    fields = {"ssh_alias", "host_key", "host_key_fingerprint", "ownership_marker", "state_root",
              "controller", "worker", "limits", "allowed_endpoints", "denied_endpoints"}
    try:
        if not isinstance(target, dict) or set(target) != fields:
            raise ValueError
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", target["ssh_alias"]) is None:
            raise ValueError
        marker = target["ownership_marker"]
        if re.fullmatch(r"[a-z][a-z0-9-]{0,47}", marker) is None:
            raise ValueError
        if target["state_root"] != f"/var/lib/forgejo-runner-fixture/{marker}":
            raise ValueError
        key_type, encoded = target["host_key"].split(" ")
        if key_type != "ssh-ed25519":
            raise ValueError
        key = base64.b64decode(encoded, validate=True)
        prefix = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
        if len(key) != len(prefix) + 32 or not key.startswith(prefix):
            raise ValueError
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("=")
        if target["host_key_fingerprint"] != fingerprint:
            raise ValueError
        account_fields = {"user", "uid", "gid", "subuid_start", "subgid_start", "subid_count"}
        for name in ("controller", "worker"):
            account = target[name]
            if not isinstance(account, dict) or set(account) != account_fields:
                raise ValueError
            if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", account["user"]) is None:
                raise ValueError
            if any(not positive(account[key]) for key in account_fields - {"user"}):
                raise ValueError
            if account["uid"] < 1000 or account["gid"] < 1000 or account["subid_count"] < 65536:
                raise ValueError
            if any(account[key] + account["subid_count"] > 2**32 for key in ("subuid_start", "subgid_start")):
                raise ValueError
        controller, worker = target["controller"], target["worker"]
        if any(controller[key] == worker[key] for key in ("user", "uid", "gid")):
            raise ValueError
        for key, identity in (("subuid_start", "uid"), ("subgid_start", "gid")):
            if overlaps(controller[key], controller["subid_count"], worker[key], worker["subid_count"]):
                raise ValueError
            for account in (controller, worker):
                for other in (controller, worker):
                    if account[key] <= other[identity] < account[key] + account["subid_count"]:
                        raise ValueError
        limits = target["limits"]
        if set(limits) != {"memory_bytes", "pids", "disk_bytes", "cpu_percent", "idle_seconds", "job_seconds"}:
            raise ValueError
        if any(not positive(value) for value in limits.values()):
            raise ValueError
        endpoints = []
        for key in ("allowed_endpoints", "denied_endpoints"):
            if not isinstance(target[key], list) or not target[key]:
                raise ValueError
            group = set()
            for endpoint in target[key]:
                if set(endpoint) != {"address", "port"}:
                    raise ValueError
                address = ipaddress.ip_address(endpoint["address"])
                if not positive(endpoint["port"]) or endpoint["port"] > 65535:
                    raise ValueError
                group.add((str(address), endpoint["port"]))
            endpoints.append(group)
        if endpoints[0] & endpoints[1]:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError, struct.error) as error:
        raise ValueError("Invalid disposable Debian fixture descriptor") from error
    return target


def validate_observation(target: dict, observed: dict, *, allow_new_fixture=False) -> dict:
    """Require an independently observed empty, administrator-owned fixture."""
    validate_target(target)
    try:
        if observed["os_id"] != "debian" or observed["os_version"] != "13":
            raise ValueError
        architecture = {"x86_64": "amd64", "aarch64": "arm64"}[observed["architecture"]]
        if observed["effective_uid"] != 0 or observed["cgroup_v2"] is not True or observed["user_namespaces"] is not True:
            raise ValueError
        marker = observed["marker"]
        new_parent = allow_new_fixture and observed.get('state_parent_exists') is False
        if new_parent:
            if marker != {'value': None, 'uid': None, 'mode': None, 'regular': False}:
                raise ValueError
        elif (marker["value"] != target["ownership_marker"] or marker["uid"] != 0
                or marker["regular"] is not True or marker["mode"] & 0o022):
            raise ValueError
        if observed["state_root_exists"] is not False or observed["state_parent_secure"] is not True:
            raise ValueError
        if any(observed["commands"].get(command) is not True for command in REQUIRED_COMMANDS):
            raise ValueError
        for name in ("controller", "worker"):
            account = target[name]
            for user in observed["users"]:
                if (user["user"] == account["user"] or user["uid"] == account["uid"]
                        or account["subuid_start"] <= user["uid"] < account["subuid_start"] + account["subid_count"]):
                    raise ValueError
            for group in observed["groups"]:
                if (group["group"] == account["user"] or group["gid"] == account["gid"]
                        or account["subgid_start"] <= group["gid"] < account["subgid_start"] + account["subid_count"]):
                    raise ValueError
            for ranges, start, identity in (("subuids", "subuid_start", "uid"),
                                             ("subgids", "subgid_start", "gid")):
                for allocation in observed[ranges]:
                    if (not positive(allocation["start"]) or not positive(allocation["count"])
                            or allocation["user"] in (account["user"], str(account["uid"]))
                            or allocation["start"] <= account[identity] < allocation["start"] + allocation["count"]
                            or overlaps(account[start], account["subid_count"], allocation["start"], allocation["count"])):
                        raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError("Disposable Debian fixture preflight failed; no setup was performed") from error
    return {**observed, "architecture": architecture}


def inspect_target(target: dict) -> dict:
    """Observe a pinned SSH host without setup, repair, registration or socket use."""
    validate_target(target)
    source = (ROOT / "scripts/forgejo_runner/host_probe.py").read_text()
    payload = source + "\nprint(json.dumps(observe(" + repr(target) + ")))\n"
    return validate_observation(target, ssh_observation(target, payload))


def ssh_observation(target: dict, payload: str, *, timeout=30, input_files=None) -> dict:
    """Pinned noninteractive transport with bounded, sanitized response handling."""
    validate_target(target)
    with tempfile.TemporaryDirectory(prefix="forgejo-runner-host-") as directory:
        known_hosts = Path(directory) / "known_hosts"
        known_hosts.write_text(target["ownership_marker"] + " " + target["host_key"] + "\n")
        known_hosts.chmod(0o600)
        argv = ["ssh", "-T"]
        for option in (
            "BatchMode=yes", "StrictHostKeyChecking=yes", "CheckHostIP=no",
            "GlobalKnownHostsFile=/dev/null", f"UserKnownHostsFile={known_hosts}",
            f"HostKeyAlias={target['ownership_marker']}", "ClearAllForwardings=yes",
            "PermitLocalCommand=no", "RequestTTY=no", "RemoteCommand=none", "ConnectTimeout=10",
            "ControlMaster=no", "ControlPath=none", "UpdateHostKeys=no",
            "ForwardAgent=no", "ForwardX11=no", "Tunnel=no", "HostKeyAlgorithms=ssh-ed25519",
        ):
            argv.extend(("-o", option))
        try:
            if input_files:
                source = payload.encode()
                if len(source) > 2 * 1024**2 or len(input_files) > 16:
                    raise ValueError('Offline fixture source or input count exceeds its bound')
                loader = ('import sys; n=int(sys.stdin.buffer.readline(32)); '
                          'assert 0<n<=2097152; '
                          'exec(compile(sys.stdin.buffer.read(n),"<fixture>","exec"))')
                argv.extend((target['ssh_alias'], 'sudo -n -- /usr/bin/python3 -c ' + shlex.quote(loader)))
                with tempfile.TemporaryFile() as stream:
                    stream.write(str(len(source)).encode() + b'\n' + source)
                    total = 0
                    for path in input_files:
                        path = Path(path).absolute()
                        if not path.resolve(strict=True).is_relative_to((ROOT / '.tmp').resolve()) or path.is_symlink():
                            raise ValueError('Offline fixture inputs must be regular local files under .tmp')
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                        with os.fdopen(fd, 'rb') as asset:
                            metadata = os.fstat(asset.fileno())
                            total += metadata.st_size
                            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                                    or metadata.st_size <= 0 or total > 2 * 1024**3):
                                raise ValueError('Offline fixture inputs exceed their bound')
                            remaining = metadata.st_size
                            while remaining:
                                chunk = asset.read(min(remaining, 1024 * 1024))
                                if not chunk:
                                    raise ValueError('Offline fixture input changed during transfer')
                                stream.write(chunk)
                                remaining -= len(chunk)
                            if asset.read(1):
                                raise ValueError('Offline fixture input grew during transfer')
                    stream.seek(0)
                    result = subprocess.run(argv, stdin=stream, text=True, capture_output=True,
                                            timeout=timeout, check=False)
            else:
                argv.extend((target['ssh_alias'], 'sudo -n -- /usr/bin/python3 -'))
                result = subprocess.run(argv, input=payload, text=True, capture_output=True,
                                        timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Disposable fixture SSH observation failed") from error
    if result.returncode:
        raise RuntimeError("Disposable fixture SSH observation failed; check supplied access and host key")
    try:
        if len(result.stdout) > 65536:
            raise ValueError('Host response exceeded its bounded output size')
        observed = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise ValueError("Disposable fixture returned invalid observation") from error
    return observed


def inspect_resources(target: dict) -> dict:
    """Run only the separately selected bounded resource experiment."""
    from scripts.forgejo_runner.host_resources import validate_probe_budget
    validate_target(target)
    validate_probe_budget(target)
    payload = "__file__ = '/fixture/scripts/forgejo_runner/host_fixture.py'\n"
    # These repository-owned sources run outside the worker under existing
    # operator fixture access. No server-installed helper or inventory input.
    for name in ('host_fixture.py', 'host_probe.py', 'host_resources.py'):
        source = (ROOT / 'scripts/forgejo_runner' / name).read_text()
        payload += 'exec(' + repr(source) + ')\n'
    probe = (ROOT / 'scripts/forgejo_runner/resource_probe.py').read_text()
    payload += 'print(json.dumps(run_resource_probe(' + repr(target) + ', observe, validate_observation, ' + repr(probe) + ')))\n'
    observed = ssh_observation(target, payload, timeout=300)
    if (not isinstance(observed, dict) or observed.get('acceptance') != 'resources-only'
            or type(observed.get('exit_code')) is not int or observed['exit_code'] not in (0, 1, 130)
            or not isinstance(observed.get('cleanup_errors'), list)):
        raise ValueError('Invalid resource experiment outcome')
    if observed['exit_code'] == 0:
        required = {'limits_verified', 'private_loopback', 'disk_bounded', 'pids_bounded'}
        result = observed.get('observations', {})
        if (observed['cleanup_errors'] or observed.get('primary_error') is not None
                or set(result) != required or any(result[key] is not True for key in required)):
            raise ValueError('Resource experiment lacks its required evidence')
    return observed


def inspect_workers(target: dict) -> dict:
    """Run the fixed offline worker experiment, separately from runner acceptance."""
    from scripts.forgejo_runner.host_worker import validate_worker_budget
    validate_target(target)
    validate_worker_budget(target)
    payload = "__file__ = '/fixture/scripts/forgejo_runner/host_fixture.py'\n"
    for name in ('host_fixture.py', 'host_probe.py', 'host_resources.py', 'host_worker.py'):
        payload += 'exec(' + repr((ROOT / 'scripts/forgejo_runner' / name).read_text()) + ')\n'
    probe = (ROOT / 'scripts/forgejo_runner/worker_probe.py').read_text()
    launcher = (ROOT / 'scripts/forgejo_runner/worker_launch.py').read_text()
    payload += 'print(json.dumps(run_image_probe(' + repr(target) + ', observe, validate_observation, '
    payload += repr(probe) + ", stage='worker-only', budget_validator=validate_worker_budget, extra_files=worker_files("
    payload += repr(target) + ', ' + repr(launcher) + '), worker_observer=observe_worker_boundary, worker_reader=read_worker_result, after_stop=worker_cleanup_errors)))\n'
    outcome = ssh_observation(target, payload, timeout=300)
    if (not isinstance(outcome, dict) or outcome.get('acceptance') != 'worker-only'
            or type(outcome.get('exit_code')) is not int or outcome['exit_code'] not in (0, 1, 130)
            or not isinstance(outcome.get('cleanup_errors'), list)):
        raise ValueError('Invalid worker experiment outcome')
    if outcome['exit_code'] == 0:
        required = {'rootless_api', 'sibling_containers', 'mapped_bind', 'private_loopback',
                    'published_loopback', 'user_manager', 'bounded_storage', 'outer_limits', 'process_boundary'}
        result = outcome.get('observations', {})
        if (outcome['cleanup_errors'] or outcome.get('primary_error') is not None
                or set(result) != required or any(result[key] is not True for key in required)):
            raise ValueError('Worker experiment lacks required evidence')
    return outcome


def inspect_network(target: dict) -> dict:
    """Controlled peer links never join or change the production host network."""
    from scripts.forgejo_runner.host_network import validate_network_target, validate_network_budget
    from scripts.forgejo_runner.host_worker import worker_files
    validate_target(target)
    validate_network_target(target)
    validate_network_budget(target)
    extra = worker_files(target, (ROOT / 'scripts/forgejo_runner/worker_launch.py').read_text())
    configuration = json.loads(extra['worker.json'])
    configuration['network'] = {'controller': target['controller']}
    extra['worker.json'] = json.dumps(configuration)
    for name in ('host_network.py', 'network_setup.py', 'network_peer.py'):
        extra[name] = (ROOT / 'scripts/forgejo_runner' / name).read_text()
    extra['offline_probe.py'] = (ROOT / 'scripts/forgejo_runner/worker_probe.py').read_text()
    payload = "__file__ = '/fixture/scripts/forgejo_runner/host_fixture.py'\n"
    for name in ('host_fixture.py', 'host_probe.py', 'host_resources.py', 'host_worker.py', 'host_network.py'):
        payload += 'exec(' + repr((ROOT / 'scripts/forgejo_runner' / name).read_text()) + ')\n'
    probe = (ROOT / 'scripts/forgejo_runner/network_probe.py').read_text()
    payload += 'print(json.dumps(run_image_probe(' + repr(target) + ', observe, validate_observation, '
    payload += repr(probe) + ", stage='network-only', budget_validator=validate_network_budget, extra_files=" + repr(extra)
    payload += ', worker_observer=lambda t,o,u: observe_network_boundary(t,o,u,observe_worker_boundary)'
    payload += ', worker_reader=read_worker_result, after_stop=lambda t: worker_cleanup_errors(t)'
    payload += "+worker_cleanup_errors({**t,'worker':t['controller']}))))\n"
    outcome = ssh_observation(target, payload, timeout=300)
    if (not isinstance(outcome, dict) or outcome.get('acceptance') != 'network-only'
            or type(outcome.get('exit_code')) is not int or outcome['exit_code'] not in (0, 1, 130)
            or not isinstance(outcome.get('cleanup_errors'), list)):
        raise ValueError('Invalid controlled network experiment outcome')
    if outcome['exit_code'] == 0:
        required = {'rootless_api', 'sibling_containers', 'mapped_bind', 'private_loopback',
                    'published_loopback', 'user_manager', 'bounded_storage', 'outer_limits', 'process_boundary',
                    'allowed_ipv4', 'allowed_ipv6', 'denied_ipv4', 'denied_ipv6', 'policy_immutable',
                    'alternate_network_modes', 'kernel_policy', 'controlled_peer'}
        observations = outcome.get('observations', {})
        if (outcome['cleanup_errors'] or outcome.get('primary_error') is not None
                or set(observations) != required or any(observations[key] is not True for key in required)):
            raise ValueError('Controlled network experiment lacks required evidence')
    return outcome


def inspect_egress(target: dict) -> dict:
    """Real public access remains within the owned finite native experiment."""
    from scripts.forgejo_runner.host_network import validate_network_target, validate_network_budget
    from scripts.forgejo_runner.host_worker import worker_files
    validate_target(target)
    validate_network_target(target)
    validate_network_budget(target)
    extra = worker_files(target, (ROOT / 'scripts/forgejo_runner/worker_launch.py').read_text())
    configuration = json.loads(extra['worker.json'])
    configuration['network'] = {'controller': target['controller']}
    extra['worker.json'] = json.dumps(configuration)
    for name in ('host_network.py', 'network_setup.py', 'network_peer.py', 'host_egress.py', 'egress_probe.py'):
        extra[name] = (ROOT / 'scripts/forgejo_runner' / name).read_text()
    extra['gateway-launch.py'] = (ROOT / 'scripts/forgejo_runner/gateway_launch.py').read_text()
    extra['offline_probe.py'] = (ROOT / 'scripts/forgejo_runner/worker_probe.py').read_text()
    payload = "__file__ = '/fixture/scripts/forgejo_runner/host_fixture.py'\n"
    for name in ('host_fixture.py', 'host_probe.py', 'host_resources.py', 'host_worker.py', 'host_network.py', 'host_egress.py'):
        payload += 'exec(' + repr((ROOT / 'scripts/forgejo_runner' / name).read_text()) + ')\n'
    probe = extra['egress_probe.py']
    payload += 'print(json.dumps(run_image_probe(' + repr(target) + ', observe, validate_observation, '
    payload += repr(probe) + ", stage='egress-only', budget_validator=validate_network_budget, extra_files=" + repr(extra)
    payload += ', worker_observer=observe_worker_boundary, worker_reader=read_worker_result'
    payload += ", after_stop=lambda t: worker_cleanup_errors(t)+worker_cleanup_errors({**t,'worker':t['controller']}))))\n"
    outcome = ssh_observation(target, payload, timeout=300)
    if (not isinstance(outcome, dict) or outcome.get('acceptance') != 'egress-only'
            or type(outcome.get('exit_code')) is not int or outcome['exit_code'] not in (0, 1, 130)
            or not isinstance(outcome.get('cleanup_errors'), list)):
        raise ValueError('Invalid public-egress experiment outcome')
    if outcome['exit_code'] == 0:
        required = {'rootless_api', 'sibling_containers', 'mapped_bind', 'private_loopback',
                    'published_loopback', 'user_manager', 'bounded_storage', 'outer_limits', 'process_boundary',
                    'allowed_ipv4', 'allowed_ipv6', 'denied_ipv4', 'denied_ipv6', 'policy_immutable',
                    'alternate_network_modes', 'kernel_policy', 'controlled_peer', 'dns', 'forgejo_https',
                    'public_http', 'public_https', 'gateway_boundary'}
        observations = outcome.get('observations', {})
        if (outcome['cleanup_errors'] or outcome.get('primary_error') is not None
                or set(observations) != required or any(observations[key] is not True for key in required)):
            raise ValueError('Public-egress experiment lacks required evidence')
    return outcome


def inspect_native_jobs(target: dict, images: Path, *, public_network=False, workload=None) -> dict:
    from scripts.forgejo_runner.host_worker import validate_job_budget, validate_workload_budget
    from scripts.forgejo_runner.native_assets import prepare
    from scripts.forgejo_runner.workload import validation_script, registry_sources
    validate_target(target)
    budget = validate_workload_budget if workload is not None else validate_job_budget
    budget(target)
    if workload is not None:
        validation_script(**workload)
        if not public_network:
            raise ValueError('Native workloads require the accepted public network boundary')
    if public_network:
        from scripts.forgejo_runner.host_network import validate_network_target
        validate_network_target(target)
    # Observe the target before preparing assets, and repeat the full host
    # preflight and reserve check immediately before remote allocation.
    payload = "__file__ = '/fixture/scripts/forgejo_runner/host_fixture.py'\n"
    for name in ('host_fixture.py', 'host_probe.py', 'host_resources.py', 'host_worker.py'):
        payload += 'exec(' + repr((ROOT / 'scripts/forgejo_runner' / name).read_text()) + ')\n'
    preflight = payload + 't=' + repr(target) + '\n'
    preflight += 'o=validate_observation(t,observe(t),allow_new_fixture=True)\n'
    preflight += budget.__name__ + "(t,host_capacity(Path(t['state_root']).parent if Path(t['state_root']).parent.exists() else Path(t['state_root']).parent.parent))\n"
    preflight += "print(json.dumps({'architecture':o['architecture']}))\n"
    architecture = ssh_observation(target, preflight)['architecture']
    scratch = ROOT / '.tmp/forgejo-runner'; scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='native-assets-', dir=scratch) as directory:
        assets, configuration = prepare(Path(directory), images, architecture, **({'workload':workload} if workload else {}))
        from scripts.forgejo_runner.host_worker import worker_files
        extra = worker_files(target, (ROOT / 'scripts/forgejo_runner/worker_launch.py').read_text(),
                             oci_assets=[image['asset'] for image in configuration['images']],
                             registry_images=sorted(registry_sources(**workload)) if workload else (),
                             storage_driver='overlay' if workload else 'vfs')
        extra['offline_probe.py'] = (ROOT / 'scripts/forgejo_runner/worker_probe.py').read_text()
        extra['job.json'] = json.dumps(configuration)
        if public_network:
            worker = json.loads(extra['worker.json'])
            worker['network'] = {'controller': target['controller']}
            extra['worker.json'] = json.dumps(worker)
            for name in ('host_network.py', 'network_setup.py', 'network_peer.py', 'host_egress.py', 'egress_probe.py'):
                extra[name] = (ROOT / 'scripts/forgejo_runner' / name).read_text()
            extra['gateway-launch.py'] = (ROOT / 'scripts/forgejo_runner/gateway_launch.py').read_text()
            for name in ('host_network.py', 'host_egress.py'):
                payload += 'exec(' + repr((ROOT / 'scripts/forgejo_runner' / name).read_text()) + ')\n'

        probe = '''import contextlib, json, pathlib, sys, tarfile
sys.dont_write_bytecode = True
for name, destination in (('source.tar', '/work/source'), ('vendor.tar', '/work/vendor')):
    pathlib.Path(destination).mkdir(mode=0o700)
    with tarfile.open('/work/input/' + name) as archive:
        archive.extractall(destination, filter='data')
sys.path[:0] = ['/work/source', '/work/vendor', '/']
from scripts.forgejo_runner.native_job import run_probe
with pathlib.Path('/work/job-progress.log').open('w', buffering=1) as progress, contextlib.redirect_stdout(progress):
    result = run_probe()
print(json.dumps(result), flush=True)
'''
        stage = 'workload-only' if workload else 'native-only' if public_network else 'job-only'
        if public_network:
            probe = probe.replace('result = run_probe()', 'import egress_probe; result = run_probe(observed=egress_probe.main())')
        payload += 'exec(' + repr((ROOT / 'scripts/forgejo_runner/host_inputs.py').read_text()) + ')\n'
        payload += 'print(json.dumps(run_image_probe(' + repr(target) + ', observe, validate_observation, ' + repr(probe)
        payload += ", stage=" + repr(stage) + ", budget_validator=" + budget.__name__ + ", extra_files=" + repr(extra)
        payload += ', assets=' + repr(assets) + ', asset_validator=validate_assets, asset_receiver=receive_asset'
        payload += ', worker_observer=observe_job_boundary, worker_reader=read_worker_result, after_stop=lambda t: worker_cleanup_errors(t)'
        payload += "+worker_cleanup_errors({**t,'worker':t['controller']}))))\n"
        outcome = ssh_observation(target, payload, timeout=target['limits']['job_seconds'] + 600,
                                  input_files=[Path(directory) / Path(name).name for name in assets])
    required = {'rootless_api', 'sibling_containers', 'mapped_bind', 'private_loopback',
                'published_loopback', 'user_manager', 'bounded_storage', 'outer_limits',
                'process_boundary', 'one_job', 'job_runtime'}
    if public_network:
        required |= {'allowed_ipv4', 'allowed_ipv6', 'denied_ipv4', 'denied_ipv6', 'policy_immutable',
                     'alternate_network_modes', 'kernel_policy', 'controlled_peer', 'dns', 'forgejo_https',
                     'public_http', 'public_https', 'gateway_boundary', 'job_public_access'}
    if workload:
        required.add('job_workload')
    if (not isinstance(outcome, dict) or outcome.get('acceptance') != stage
            or type(outcome.get('exit_code')) is not int or outcome['exit_code'] not in (0, 1, 130)
            or not isinstance(outcome.get('cleanup_errors'), list)):
        raise ValueError('Invalid native job experiment outcome')
    if outcome['exit_code'] == 0:
        result = outcome.get('observations', {})
        if (outcome['cleanup_errors'] or outcome.get('primary_error') is not None
                or set(result) != required or any(result[key] is not True for key in required)):
            raise ValueError('Native job experiment lacks required evidence')
    return {**outcome, 'architecture': architecture, 'source_tree': configuration['source_tree'],
            **({'workload':workload} if workload else {})}


def run(images: Path | None, target: Path | None, *, preflight_only: bool = False,
        resource_probe_only: bool = False, worker_probe_only: bool = False,
        job_probe_only: bool = False, network_probe_only: bool = False, egress_probe_only: bool = False,
        native_probe_only: bool = False, native_workload=None, suite=None) -> int:
    """Separate observational prerequisites from the unfinished containment gate."""
    if target is None:
        raise ValueError("An explicit disposable Debian host fixture descriptor is required")
    target = target.resolve()
    if not target.is_relative_to((ROOT / ".tmp").resolve()):
        raise ValueError("Disposable host descriptors must be local inputs under .tmp")
    try:
        if target.stat().st_size > 65536:
            raise ValueError("Host descriptor exceeds its bounded input size")
        descriptor = json.loads(target.read_text())
    except (OSError, ValueError) as error:
        raise ValueError("Cannot load disposable host descriptor") from error
    validate_target(descriptor)
    if native_probe_only or native_workload or suite:
        if native_workload and suite or native_probe_only and (native_workload or suite):
            raise ValueError('Conflicting native workload selection')
        selection = {'selector':native_workload} if native_workload else {'suite':suite} if suite else None
        if preflight_only or resource_probe_only or worker_probe_only or job_probe_only or network_probe_only or egress_probe_only:
            raise ValueError('Combined native probe cannot be mixed with isolated experiments')
        if images is None or not images.resolve().is_relative_to((ROOT / '.tmp').resolve()):
            raise ValueError('Native acceptance requires a reviewed image descriptor under .tmp')
        if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
            raise ValueError('Native acceptance requires a clean committed source candidate')
        tree = subprocess.check_output(['git', 'write-tree'], cwd=ROOT, text=True).strip()
        result = inspect_native_jobs(descriptor, images, public_network=True, workload=selection)
        if (tree != result['source_tree'] or tree != subprocess.check_output(['git', 'write-tree'], cwd=ROOT, text=True).strip()
                or subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()):
            raise RuntimeError('Native acceptance source changed; evidence is invalid')
        with tempfile.NamedTemporaryFile(mode='w', prefix='host-native-workload-outcome-' if selection else 'host-native-outcome-', suffix='.json', dir=target.parent, delete=False) as evidence:
            json.dump(result, evidence, indent=2); evidence.write('\n')
        print(f'Combined native outcome saved to {evidence.name}')
        if result['exit_code']:
            raise RuntimeError('Combined native probe failed; inspect saved outcome and any retained allocation')
        print('Combined native job and network checks passed; full workload acceptance remains pending')
        return 0
    if network_probe_only or egress_probe_only:
        if preflight_only or resource_probe_only or worker_probe_only or job_probe_only or images:
            raise ValueError('Network probe cannot be combined with other experiments')
        if network_probe_only and egress_probe_only:
            raise ValueError('Controlled and public network experiments cannot be combined')
        if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
            raise ValueError('Network acceptance requires a clean committed source candidate')
        source_tree = subprocess.check_output(['git', 'write-tree'], cwd=ROOT, text=True).strip()
        result = inspect_egress(descriptor) if egress_probe_only else inspect_network(descriptor)
        if (source_tree != subprocess.check_output(['git', 'write-tree'], cwd=ROOT, text=True).strip()
                or subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()):
            raise RuntimeError('Network probe source changed; acceptance evidence is invalid')
        result['source_tree'] = source_tree
        with tempfile.NamedTemporaryFile(mode='w', prefix='host-egress-outcome-' if egress_probe_only else 'host-network-outcome-', suffix='.json',
                                         dir=target.parent, delete=False) as evidence:
            json.dump(result, evidence, indent=2); evidence.write('\n')
        print(f'Host network outcome saved to {evidence.name}')
        if result['exit_code']:
            raise RuntimeError('Controlled network probe failed; inspect saved outcome and any retained allocation')
        print('Network policy passed and owned state removed; full runner acceptance remains pending')
        return 0
    if job_probe_only:
        if preflight_only or resource_probe_only or worker_probe_only:
            raise ValueError('Job probe cannot be combined with other experiments')
        if images is None or not images.resolve().is_relative_to((ROOT / '.tmp').resolve()):
            raise ValueError('Native job requires a local reviewed image descriptor under .tmp')
        result = inspect_native_jobs(descriptor, images)
        with tempfile.NamedTemporaryFile(mode='w', prefix='host-job-outcome-', suffix='.json',
                                         dir=target.parent, delete=False) as evidence:
            json.dump(result, evidence, indent=2); evidence.write('\n')
        print(f'Host job outcome saved to {evidence.name}')
        if result['exit_code']:
            raise RuntimeError('Native job probe failed; inspect saved outcome and any retained allocation')
        print('Offline native job passed and owned state removed; full runner acceptance is still pending')
        return 0
    if worker_probe_only:
        if preflight_only or images or resource_probe_only:
            raise ValueError('Worker probe cannot be combined with other experiments')
        result = inspect_workers(descriptor)
        with tempfile.NamedTemporaryFile(mode='w', prefix='host-worker-outcome-', suffix='.json',
                                         dir=target.parent, delete=False) as evidence:
            json.dump(result, evidence, indent=2)
            evidence.write('\n')
        print(f'Host worker outcome saved to {evidence.name}')
        if result['exit_code']:
            if result.get('cleanup_errors'):
                raise RuntimeError('Host worker cleanup failed; inspect retained allocation and saved outcome')
            raise RuntimeError('Host worker probe failed; owned state removed; see saved outcome')
        print('Offline rootless worker probe passed and owned state removed; runner acceptance is still pending')
        return 0
    if resource_probe_only:
        if preflight_only or images:
            raise ValueError('Resource probe cannot be combined with other experiments')
        from scripts.forgejo_runner.host_resources import validate_probe_budget
        validate_probe_budget(descriptor)
        result = inspect_resources(descriptor)
        with tempfile.NamedTemporaryFile(mode='w', prefix='host-resource-outcome-', suffix='.json',
                                         dir=target.parent, delete=False) as evidence:
            json.dump(result, evidence, indent=2)
            evidence.write('\n')
        print(f'Host resource outcome saved to {evidence.name}')
        if result['exit_code']:
            if result.get('cleanup_errors'):
                raise RuntimeError('Host resource cleanup failed; inspect retained allocation and saved outcome')
            raise RuntimeError('Host resource probe failed; owned state removed; see saved outcome')
        print('Host resource probe passed and owned state removed; runner containment was not tested')
        return 0
    if not preflight_only:
        # Do not touch a target until combined acceptance is implemented. A
        # successful observation alone must never satisfy the host fixture gate.
        raise RuntimeError("Host containment acceptance is not implemented; use --preflight-only for observation")
    result = inspect_target(descriptor)
    print(f"Disposable Debian preflight passed ({result['architecture']}); containment was not tested")
    return 0
