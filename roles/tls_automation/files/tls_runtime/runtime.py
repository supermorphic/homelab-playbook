"""No-argument host entrypoints. Paths and privileged operations are code-owned."""
from contextlib import contextmanager, nullcontext
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time

from .certificates import validate_certificate, validate_historical_certificate
from .policy import (
    ACL_GROUP,
    ACL_GROUP_OBJ,
    ACL_MASK,
    ACL_OTHER,
    ACL_UNDEFINED_ID,
    ACL_USER,
    ACL_USER_OBJ,
    parse_policy,
    read_bounded,
    read_posix_acl as _read_posix_acl,
    require_no_default_acl,
    require_no_named_access_acl,
    secure_path,
)
from .publication import PublicationError, Publisher
from .unit_contract import (SERVICE_ARRAY_PROPERTIES, exec_start_matches,
                            merge_service_arrays, service_array_query)

CONFIG = Path("/etc/homelab-tls/config.json")
STATE = Path("/var/lib/homelab-tls")
PRIVATE = STATE / "private"
PUBLISHED = Path("/etc/caddy/tls/infra")
PUBLICATION_PRIVATE = Path("/etc/caddy/tls/.homelab-tls-private")
ISSUER_STATE = Path("/var/lib/homelab-tls-issuer")
JOURNAL = PUBLICATION_PRIVATE / "transaction.json"
LOCK = PRIVATE / "coordinator.lock"
STATUS = PRIVATE / "status.json"
ADAPTER = Path("/usr/local/libexec/homelab-tls-caddy")
LEGO = "/usr/local/libexec/lego-5.4.1"
ISSUER_UNIT = "homelab-tls-issuer.service"
RENEW_UNIT = "homelab-tls-renew.service"
TOKEN_SOURCE = Path("/etc/homelab-tls/cloudflare-token")
TOKEN_RUNTIME = "/run/credentials/homelab-tls-issuer.service/cloudflare-token"
CLEAN_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/"}
GENERATION = re.compile(r"generation-[0-9a-f]{32}\Z")
PUBLISHED_FILES = ("fullchain.pem", "privkey.pem")
UNIT_CONTRACTS = {
    ISSUER_UNIT: {
        "User": "svc-acme",
        "Group": "svc-acme",
        "SupplementaryGroups": "",
        "Type": "oneshot",
        "NoNewPrivileges": "yes",
        "ProtectSystem": "strict",
        "PrivateTmp": "yes",
        "ReadWritePaths": str(ISSUER_STATE),
        "LoadCredential": "cloudflare-token:" + str(TOKEN_SOURCE),
        "FragmentPath": "/etc/systemd/system/" + ISSUER_UNIT,
        "DropInPaths": "",
        "ExecCondition": "",
        "ExecStartPre": "",
        "ExecStartPost": "",
        "ExecStart": "/usr/local/libexec/homelab-tls-issue",
    },
    RENEW_UNIT: {
        "User": "root",
        "Group": "root",
        "SupplementaryGroups": "",
        "Type": "oneshot",
        "NoNewPrivileges": "yes",
        "AmbientCapabilities": "cap_setuid",
        "ProtectSystem": "strict",
        "PrivateTmp": "yes",
        "ReadWritePaths": str(STATE) + " /etc/caddy /var/lib/homelab-reverse-proxy",
        "LoadCredential": "",
        "FragmentPath": "/etc/systemd/system/" + RENEW_UNIT,
        "DropInPaths": "",
        "ExecCondition": "",
        "ExecStartPre": "",
        "ExecStartPost": "",
        "ExecStart": "/usr/local/libexec/homelab-tls-reconcile",
    },
}

def run_fixed(argv, timeout=60, environment=None, capture=False):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=environment or CLEAN_ENV,
                            cwd="/", timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("fixed host operation failed")
    return result.stdout if capture else None


def run_issuer(argv, timeout=900, environment=None):
    """Keep a bounded private output tail; never relay child output to callers."""
    directory = os.open(ISSUER_STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("expected private issuer diagnostic directory")
        require_no_named_access_acl(directory)
        require_no_default_acl(directory)
        descriptor, temporary = tempfile.mkstemp(prefix=".last-issue-", dir=ISSUER_STATE)
        try:
            with os.fdopen(descriptor, "w+b", buffering=0) as diagnostic:
                os.fchmod(diagnostic.fileno(), 0o600)
                require_no_named_access_acl(diagnostic.fileno())
                # Replace rather than truncate an existing path, which may be a link.
                os.replace(temporary, "last-issue.log", dst_dir_fd=directory)
                deadline = time.monotonic() + timeout
                with subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      env=environment or CLEAN_ENV, cwd="/") as process:
                    try:
                        tail = b""
                        timed_out = False
                        os.set_blocking(process.stdout.fileno(), False)
                        with selectors.DefaultSelector() as selector:
                            selector.register(process.stdout, selectors.EVENT_READ)
                            while selector.get_map():
                                remaining = deadline - time.monotonic()
                                if remaining <= 0 and not timed_out:
                                    timed_out = True
                                    if process.poll() is None:
                                        process.kill()
                                    process.wait()
                                # After termination, retain bytes already in the pipe.
                                # Do not wait for a descriptor inherited by another process.
                                if not selector.select(0 if timed_out else min(remaining, 0.2)):
                                    if timed_out:
                                        break
                                    continue
                                chunk = os.read(process.stdout.fileno(), 8192)
                                if not chunk:
                                    selector.unregister(process.stdout)
                                    break
                                tail = (tail + chunk)[-65536:]
                                diagnostic.seek(0)
                                diagnostic.write(tail)
                                diagnostic.truncate()
                        if timed_out:
                            raise RuntimeError("issuer timed out; inspect private diagnostic")
                        try:
                            code = process.wait(timeout=max(0, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired:
                            raise RuntimeError("issuer timed out; inspect private diagnostic") from None
                        if code:
                            raise RuntimeError(f"issuer exited with code {code}; inspect private diagnostic")
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait()
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
    finally:
        os.close(directory)


def _descriptor_read_only(descriptor):
    return bool(os.fstatvfs(descriptor).f_flag & os.ST_RDONLY)


def _expected_service_acl(service_uid, permissions):
    return (
        (ACL_USER_OBJ, permissions, ACL_UNDEFINED_ID),
        (ACL_USER, permissions, service_uid),
        (ACL_GROUP_OBJ, 0, ACL_UNDEFINED_ID),
        (ACL_MASK, permissions, ACL_UNDEFINED_ID),
        (ACL_OTHER, 0, ACL_UNDEFINED_ID),
    )


def _fallback_owners_match(parent_info, credential_info, *, service_uid,
                           service_gid, trusted_gid):
    return (
        parent_info.st_uid == service_uid
        and credential_info.st_uid == service_uid
        and parent_info.st_gid == credential_info.st_gid
        and parent_info.st_gid in {trusted_gid, service_gid}
    )


def validate_runtime_credential(path, service_uid, service_gid, *,
                                credential_root=Path("/run/credentials"),
                                trusted_uid=0, trusted_gid=0):
    """Validate systemd's ACL-first or read-only ownership-fallback boundary.

    The function opens each fixed component with ``O_NOFOLLOW`` and inspects
    only descriptor metadata. It never reads credential bytes.
    """
    path = Path(path)
    credential_root = Path(credential_root)
    expected_parent = credential_root / ISSUER_UNIT
    if (not credential_root.is_absolute() or path != expected_parent / "cloudflare-token"):
        raise ValueError("unexpected systemd credential path")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(credential_root, flags)
    parent_fd = None
    credential_fd = None
    try:
        root_info = os.fstat(root_fd)
        if (not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != trusted_uid or root_info.st_gid != trusted_gid
                or stat.S_IMODE(root_info.st_mode) != 0o755):
            raise ValueError("unsafe systemd credential root")
        parent_fd = os.open(ISSUER_UNIT, flags, dir_fd=root_fd)
        credential_fd = os.open(
            "cloudflare-token", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        parent_info = os.fstat(parent_fd)
        credential_info = os.fstat(credential_fd)
        if (not stat.S_ISDIR(parent_info.st_mode)
                or not stat.S_ISREG(credential_info.st_mode)
                or credential_info.st_nlink != 1):
            raise ValueError("unsafe systemd credential types")

        acl_owned = (
            parent_info.st_uid == trusted_uid
            and parent_info.st_gid == trusted_gid
            and stat.S_IMODE(parent_info.st_mode) == 0o550
            and credential_info.st_uid == trusted_uid
            and credential_info.st_gid == trusted_gid
            and stat.S_IMODE(credential_info.st_mode) == 0o440
            and _read_posix_acl(parent_fd) == _expected_service_acl(service_uid, 5)
            and _read_posix_acl(credential_fd) == _expected_service_acl(service_uid, 4)
        )
        fallback_owned = (
            _fallback_owners_match(
                parent_info, credential_info,
                service_uid=service_uid, service_gid=service_gid,
                trusted_gid=trusted_gid,
            )
            and stat.S_IMODE(parent_info.st_mode) == 0o500
            and stat.S_IMODE(credential_info.st_mode) == 0o400
            and _read_posix_acl(parent_fd) is None
            and _read_posix_acl(credential_fd) is None
            and _descriptor_read_only(parent_fd)
            and _descriptor_read_only(credential_fd)
        )
        if not (acl_owned or fallback_owned):
            raise ValueError("unsafe systemd credential access boundary")
    finally:
        if credential_fd is not None:
            os.close(credential_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def _systemd_properties(raw):
    try:
        lines = raw.decode("ascii").splitlines()
    except (AttributeError, UnicodeDecodeError) as error:
        raise ValueError("effective TLS unit properties are malformed") from error
    properties = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not key or key in properties:
            raise ValueError("effective TLS unit properties are malformed")
        properties[key] = value
    return properties


def inspect_unit(unit, contract):
    properties = ",".join(key for key in contract if key not in SERVICE_ARRAY_PROPERTIES)
    raw = run_fixed([
        "/usr/bin/systemctl", "show", unit, "--all", "--property=" + properties,
    ], capture=True)
    actual = _systemd_properties(raw)
    typed = run_fixed(service_array_query(unit), timeout=60, capture=True)
    actual = merge_service_arrays(actual, unit, typed)
    if set(actual) != set(contract):
        raise ValueError("effective TLS unit properties are incomplete")
    for key, expected in contract.items():
        if key == "ExecStart":
            if not exec_start_matches(actual[key], expected):
                raise ValueError("unexpected effective TLS service command")
        elif actual[key] != expected:
            raise ValueError("unexpected effective TLS unit configuration")
    fragment = Path(contract["FragmentPath"])
    info = secure_path(fragment)
    if stat.S_IMODE(info.st_mode) != 0o644 or info.st_gid != 0:
        raise ValueError("unexpected TLS unit fragment metadata")


def _directory_without_extra_acl(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("ACL boundary is not a directory")
        require_no_named_access_acl(descriptor)
        require_no_default_acl(descriptor)
        return info
    finally:
        os.close(descriptor)


def validate_publication_acl_roots(private=PUBLICATION_PRIVATE, published=PUBLISHED):
    """Reject ACLs that could leak new private or published material."""
    _directory_without_extra_acl(private)
    _directory_without_extra_acl(published)


def load_policy():
    info = secure_path(CONFIG)
    if stat.S_IMODE(info.st_mode) != 0o640 or info.st_gid != pwd.getpwnam("svc-acme").pw_gid:
        raise ValueError("incorrect public policy read boundary")
    return parse_policy(read_bounded(CONFIG.parent, CONFIG.name, 0, 32768))


def issuer_command(policy):
    # Contract: upstream v5.4.1 cmd/internal/flags and cmd/cmd_run_renew.go.
    # No renew-days: upstream uses ARI, then remaining 1/3 lifetime (1/2 if <=10d).
    return [LEGO, "run", "--accept-tos", "--email", policy.email,
            "--server", "https://acme-v02.api.letsencrypt.org/directory",
            "--path", str(ISSUER_STATE), "--cert.name", "caddy",
            "--domains", policy.sans[0], "--dns", "cloudflare", "--force-cert-domains",
            "--dns.resolvers", "1.1.1.1:53",
            "--no-random-sleep", "--ari-wait-to-renew-duration", "0s", "--http-timeout", "30"]


def issue(policy):
    account = pwd.getpwnam("svc-acme")
    if os.geteuid() != account.pw_uid or os.geteuid() == 0:
        raise ValueError("issuer requires its dedicated account")
    secure_path(LEGO)
    secure_path(ISSUER_STATE, account.pw_uid, directory=True)
    validate_runtime_credential(Path(TOKEN_RUNTIME), account.pw_uid, account.pw_gid)
    environment = dict(CLEAN_ENV, CF_DNS_API_TOKEN_FILE=TOKEN_RUNTIME)
    run_issuer(issuer_command(policy), timeout=900, environment=environment)


def inspect_host(policy):
    account = pwd.getpwnam("svc-acme")
    if account.pw_uid == 0 or account.pw_gid == 0 or account.pw_gid == policy.reader_gid:
        raise ValueError("issuer identity overlaps privileged/read boundary")
    if set(os.getgrouplist("svc-acme", account.pw_gid)) != {account.pw_gid}:
        raise ValueError("issuer must not have supplementary groups")
    if grp.getgrnam("caddy").gr_gid != policy.reader_gid:
        raise ValueError("TLS reader must match installed Caddy group")
    for path, mode, gid in [(STATE, 0o750, policy.reader_gid), (PRIVATE, 0o700, 0),
                            (PUBLICATION_PRIVATE, 0o700, 0),
                            (PUBLISHED, 0o750, policy.reader_gid)]:
        info = secure_path(path, directory=True)
        if stat.S_IMODE(info.st_mode) != mode or info.st_gid != gid:
            raise ValueError("incorrect coordinator directory permissions")
    validate_publication_acl_roots()
    _directory_without_extra_acl(PRIVATE)
    info = secure_path(ISSUER_STATE, account.pw_uid, directory=True)
    if stat.S_IMODE(info.st_mode) != 0o700 or info.st_gid != account.pw_gid:
        raise ValueError("incorrect issuer state permissions")
    for path in [TOKEN_SOURCE, LOCK]:
        info = secure_path(path)
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("incorrect private file permissions")
    secure_path(LEGO)
    info = secure_path(ADAPTER)
    if not info.st_mode & stat.S_IXUSR:
        raise ValueError("Caddy integration adapter is unavailable")
    for unit, contract in UNIT_CONTRACTS.items():
        inspect_unit(unit, contract)


@contextmanager
def locked(shared=False):
    secure_path(LOCK)
    fd = os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another TLS operation holds the coordinator lock") from exc
        yield
    finally:
        os.close(fd)


def start_issuer():
    try:
        run_fixed(["/usr/bin/systemctl", "start", ISSUER_UNIT], timeout=960)
    except (subprocess.TimeoutExpired, RuntimeError):
        # Start failure must not leave a writer active while root snapshots output.
        run_fixed(["/usr/bin/systemctl", "stop", ISSUER_UNIT], timeout=60)
        raise


def snapshot():
    account = pwd.getpwnam("svc-acme")
    secure_path(ISSUER_STATE, account.pw_uid, directory=True)
    raw = run_fixed(
        ["/usr/bin/systemctl", "show", ISSUER_UNIT,
         "--property=ActiveState", "--value"],
        timeout=60, capture=True,
    )
    if raw.strip() not in (b"inactive", b"failed"):
        raise RuntimeError("issuer has not stopped")
    return (read_bounded(ISSUER_STATE, "certificates/caddy.crt", account.pw_uid, 262144),
            read_bounded(ISSUER_STATE, "certificates/caddy.key", account.pw_uid, 32768))


def verify_endpoint(endpoint, fingerprint, context=None):
    context = context or ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise ValueError("TLS verification cannot be disabled")
    with socket.create_connection((endpoint["address"], endpoint["port"]), timeout=10) as connection:
        with context.wrap_socket(connection, server_hostname=endpoint["hostname"]) as tls:
            actual = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
            if actual != fingerprint:
                raise ValueError("served leaf fingerprint differs from published certificate")


def verify_endpoints(policy, fingerprint):
    for endpoint in policy.endpoints:
        verify_endpoint(endpoint, fingerprint)


def read_published_directory(path, owner_uid, reader_gid):
    """Read one exact immutable generation after checking its public boundary."""
    path = Path(path)
    root = path.parent
    root_info = _directory_without_extra_acl(root)
    generation_info = _directory_without_extra_acl(path)
    if (not GENERATION.fullmatch(path.name)
            or not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != owner_uid or root_info.st_gid != reader_gid
            or stat.S_IMODE(root_info.st_mode) != 0o750
            or not stat.S_ISDIR(generation_info.st_mode)
            or generation_info.st_uid != owner_uid
            or generation_info.st_gid != reader_gid
            or stat.S_IMODE(generation_info.st_mode) != 0o750):
        raise ValueError("invalid published generation metadata")
    if {entry.name for entry in path.iterdir()} != set(PUBLISHED_FILES):
        raise ValueError("published generation has unexpected entries")
    try:
        return tuple(
            read_bounded(
                path, filename, owner_uid, maximum,
                expected_gid=reader_gid, expected_mode=0o640,
                reject_named_acl=True,
            )
            for filename, maximum in zip(PUBLISHED_FILES, (262144, 32768))
        )
    except OSError as error:
        raise ValueError("published generation file is unsafe") from error


def read_published_generation(root, owner_uid, reader_gid):
    """Resolve and read the exact generation named by the stable current link."""
    root = Path(root)
    root_info = _directory_without_extra_acl(root)
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != owner_uid
            or root_info.st_gid != reader_gid
            or stat.S_IMODE(root_info.st_mode) != 0o750):
        raise ValueError("invalid published certificate root")
    current = root / "current"
    current_info = current.lstat()
    if not stat.S_ISLNK(current_info.st_mode) or current_info.st_uid != owner_uid:
        raise ValueError("invalid current certificate link")
    target = os.readlink(current)
    if not GENERATION.fullmatch(target):
        raise ValueError("invalid current generation target")
    return read_published_directory(root / target, owner_uid, reader_gid)


def active_fingerprint(policy):
    fullchain, private_key = read_published_generation(PUBLISHED, 0, policy.reader_gid)
    return validate_certificate(fullchain, private_key, policy.sans, 0)


@contextmanager
def publication_locked(policy):
    from .caddy import Deployment
    deployment = Deployment(None, policy)
    with deployment.locked():
        yield deployment


def publisher_for(policy, deployment=None):
    from .caddy import Deployment
    deployment = deployment or Deployment(None, policy)
    def validate(path):
        # Only immutable generations beneath the fixed published root may use
        # historical validity. Candidate snapshots remain current-valid.
        path = Path(path)
        if path.parent == PUBLISHED and GENERATION.fullmatch(path.name):
            fullchain, private_key = read_published_directory(
                path, 0, policy.reader_gid
            )
            return validate_historical_certificate(
                fullchain, private_key, policy.sans
            )
        return validate_certificate(read_bounded(path, "fullchain.pem", 0, 262144),
                                    read_bounded(path, "privkey.pem", 0, 32768), policy.sans, 0)

    def preflight(path):
        secure_path(ADAPTER)
        fullchain, private_key = read_published_directory(path, 0, policy.reader_gid)
        validate_certificate(fullchain, private_key, policy.sans, 3600)
        deployment.operation("validate", path)

    def reload_and_verify(fingerprint):
        secure_path(ADAPTER)
        if not fingerprint:
            # Fixed adapter must remove the new route and prove it is inactive.
            deployment.operation("deactivate")
        else:
            deployment.operation("reload")
            verify_endpoints(policy, fingerprint)

    publisher = Publisher(PUBLISHED, JOURNAL, 0, policy.reader_gid, validate, reload_and_verify,
                          preflight, prepare=deployment.prepare)
    publisher.publication_context = deployment.locked
    return publisher


def reconcile(publisher, issue_operation, snapshot_operation, publication_context=nullcontext):
    result = {
        "issuance": "not_run",
        "publication": "failed",
        "activation_failed": False,
        "restoration_failed": False,
    }
    try:
        with publication_context():
            recovered = publisher.recover()
        if isinstance(recovered, dict):
            result.update(recovered)
            return result
    except PublicationError as error:
        result["activation_failed"] = error.activation_failed
        result["restoration_failed"] = error.restoration_failed
        return result
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return result
    result["issuance"] = "ok"
    try:
        issue_operation()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        result["issuance"] = "failed"
    try:
        fullchain, private_key = snapshot_operation()
        with publication_context():
            result["publication"] = "changed" if publisher.publish(fullchain, private_key) else "unchanged"
    except PublicationError as error:
        result["activation_failed"] = error.activation_failed
        result["restoration_failed"] = error.restoration_failed
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    return result


def write_status(result):
    """Atomically replace the current bounded phase status."""
    fd, temporary = tempfile.mkstemp(prefix="status-", dir=PRIVATE)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(json.dumps(result, sort_keys=True).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, STATUS)
        directory = os.open(PRIVATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.lexists(temporary):
            os.unlink(temporary)


def validate_status_directory():
    """Prove the private write boundary before replacing any attempt status."""
    secure_path(PRIVATE, directory=True)
    info = _directory_without_extra_acl(PRIVATE)
    if (info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("incorrect private status directory permissions")


def renew_attempt():
    """Run under the exclusive lock; interruption leaves a current attempt."""
    validate_status_directory()
    result = {"attempt": "in_progress", "preflight": "not_run",
              "issuance": "not_run", "publication": "not_run",
              "activation_failed": False, "restoration_failed": False}
    write_status(result)
    try:
        policy = load_policy()
        inspect_host(policy)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        result.update(attempt="failed", preflight="failed")
        write_status(result)
        raise
    result["preflight"] = "ok"
    write_status(result)
    publisher = publisher_for(policy)
    result.update(reconcile(publisher, start_issuer, snapshot, publisher.publication_context))
    failed = result["issuance"] == "failed" or result["publication"] == "failed"
    result["attempt"] = "failed" if failed else "completed"
    # Status contains only fixed phase outcomes, never private issuer output.
    write_status(result)
    print(json.dumps(result, sort_keys=True))
    return int(failed)


def observe(policy):
    inspect_host(policy)
    with publication_locked(policy) as deployment:
        if os.path.lexists(JOURNAL):
            raise RuntimeError("publication recovery is pending")
        deployment.a.verify(inherited=True)
        verify_endpoints(policy, active_fingerprint(policy))


def main(action, arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments or action not in ("issue", "renew", "verify"):
        print("fixed TLS entrypoints accept no arguments", file=sys.stderr)
        return 2
    try:
        if action != "issue" and os.geteuid() != 0:
            raise ValueError("coordinator and verification require root")
        os.umask(0o077)
        if action == "issue":
            issue(load_policy())
        else:
            with locked(shared=action == "verify"):
                if action == "verify":
                    observe(load_policy())
                else:
                    return renew_attempt()
        print("TLS " + action + " completed")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        print("TLS " + action + " failed; inspect configuration, unit metadata and phase status", file=sys.stderr)
        if action == "issue":
            print("If lego started, inspect /var/lib/homelab-tls-issuer/last-issue.log locally; "
                  "it may contain sensitive output", file=sys.stderr)
        return 1
