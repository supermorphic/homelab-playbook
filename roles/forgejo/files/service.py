"""Fixed host lifecycle for the rootless Forgejo deployment."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import socket
import stat
import subprocess
import time
import uuid
import urllib.error
import urllib.request

STATE = Path("/var/lib/svc-forgejo/forgejo")
HELPERS = Path("/usr/local/libexec/forgejo")
LOCK = Path("/run/forgejo/operation.lock")
ACCOUNT = "svc-forgejo"
REQUIRED_FILES = (
    "manifest.json", "config/app.ini", "config/database-password", "config/secret-key",
    "config/internal-token", "config/lfs-jwt-secret", "config/oauth2-jwt-secret",
    "credentials/postgres-admin-password", "credentials/postgres.env",
    "credentials/admin.json", "credentials/rclone.conf", "quadlets/forgejo.network",
    "quadlets/forgejo-postgres.container", "quadlets/forgejo.container",
)
STABLE_INPUTS = (
    "config/database-password", "config/secret-key", "config/internal-token",
    "config/lfs-jwt-secret", "config/oauth2-jwt-secret", "credentials/postgres-admin-password",
)


def regular(path: Path, *, private: bool = False, owner: int | None = None):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(f"expected regular file: {path.name}")
    if owner is not None and metadata.st_uid != owner:
        raise ValueError(f"unexpected file owner: {path.name}")
    forbidden = 0o077 if private else 0o022
    if metadata.st_mode & forbidden:
        raise ValueError(f"unsafe file permissions: {path.name}")


def validate_candidate(candidate: Path, allocation: dict) -> dict:
    metadata = candidate.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ValueError("candidate must be a private administrator-owned directory")
    names = set()
    for path in candidate.rglob("*"):
        item = path.lstat()
        if item.st_uid != os.geteuid() or item.st_mode & 0o022 or path.is_symlink():
            raise ValueError("candidate includes unsafe ownership, mode or link")
        if stat.S_ISREG(item.st_mode):
            if item.st_nlink != 1:
                raise ValueError("candidate includes a multiply linked file")
            names.add(str(path.relative_to(candidate)))
        elif not stat.S_ISDIR(item.st_mode):
            raise ValueError("candidate includes unsupported file type")
    if names != set(REQUIRED_FILES):
        raise ValueError("candidate contents differ from the fixed file contract")
    manifest = json.loads((candidate / "manifest.json").read_text())
    if manifest.get("schema") != 1 or manifest.get("allocation") != allocation:
        raise ValueError("candidate foundation allocation differs from the host")
    if not isinstance(manifest.get("hostname"), str) or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.-]*", manifest["hostname"]) is None:
        raise ValueError("invalid hostname")
    port = manifest.get("backend_port")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid backend port")
    network = ipaddress.ip_network(manifest["proxy_source"], strict=True)
    if network.num_addresses != 1:
        raise ValueError("proxy trust must select exactly one address")
    patterns = {
        "image": r"codeberg[.]org/forgejo/forgejo:15[.]\d+[.]\d+-rootless@sha256:[0-9a-f]{64}",
        "postgres_image": r"docker[.]io/library/postgres:17[.]\d+@sha256:[0-9a-f]{64}",
        "rclone_image": r"docker[.]io/rclone/rclone:\d+[.]\d+[.]\d+@sha256:[0-9a-f]{64}",
    }
    for key, pattern in patterns.items():
        if not isinstance(manifest.get(key), str) or re.fullmatch(pattern, manifest[key]) is None:
            raise ValueError("runtime image does not match the supported immutable pin")
    timeout = manifest.get("health_timeout_seconds")
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise ValueError("health timeout is outside its bound")
    return manifest


@contextmanager
def operation_lock(path: Path, timeout_seconds: float):
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("service operation lock timed out") from None
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield
    finally:
        os.close(descriptor)


def check_stable_inputs(candidate: Path, targets: dict[str, Path]):
    for name in STABLE_INPUTS:
        destination = targets[name]
        if destination.exists() or destination.is_symlink():
            regular(destination, private=True)
            if safe_read(destination, private=True) != safe_read(candidate / name):
                raise ValueError("stable application keys or database credentials need explicit migration")


def check_database(database: Path):
    version = database / "PG_VERSION"
    if version.exists() or version.is_symlink():
        regular(version, private=True)
        if safe_read(version, private=True).decode().strip() != "17":
            raise ValueError("existing database requires PostgreSQL major migration")
    elif database.exists() and any(database.iterdir()):
        raise ValueError("existing database storage has no valid major-version marker")


def check_parent_chain(path: Path):
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("publication parent includes a link or non-directory")


def open_directory(path: Path, *, create: bool = False) -> int:
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def safe_read(path: Path, *, private: bool = False, owner: int | None = None) -> bytes:
    parent = open_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            item = os.fstat(stream.fileno())
            forbidden = 0o077 if private else 0o022
            if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1
                    or item.st_mode & forbidden or owner is not None and item.st_uid != owner):
                raise ValueError("unsafe deployment input metadata")
            return stream.read()
    finally:
        os.close(parent)


def atomic_write(path: Path, content: bytes, *, mode: int, uid: int, gid: int):
    parent = open_directory(path.parent)
    name = ".forgejo-" + uuid.uuid4().hex
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError("publication destination is not a regular file")
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             mode, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
            os.fsync(stream.fileno())
        os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def publish_files(candidate: Path, targets: dict[str, Path], allocation: dict) -> bool:
    changed = False
    for name, target in targets.items():
        check_parent_chain(target.parent)
        if target.exists() or target.is_symlink():
            regular(target)
    for name, target in targets.items():
        protected = name.startswith(("config/", "credentials/"))
        mode = 0o600 if protected else 0o640
        uid = allocation["uid"] if protected else os.geteuid()
        gid = allocation["gid"]
        content = safe_read(candidate / name)
        if target.exists():
            item = target.stat()
            if safe_read(target) == content and stat.S_IMODE(item.st_mode) == mode and item.st_uid == uid and item.st_gid == gid:
                continue
        os.close(open_directory(target.parent, create=True))
        atomic_write(target, content, mode=mode, uid=uid, gid=gid)
        changed = True
    return changed


def execute(argv: list[str], *, input_text: str | None = None, check: bool = True,
            timeout: int = 120, environment: dict | None = None):
    try:
        result = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                                check=False, timeout=timeout, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"{Path(argv[0]).name} could not complete") from error
    if check and result.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} exited {result.returncode}")
    return result


def account_allocation() -> dict:
    account = pwd.getpwnam(ACCOUNT)
    group = grp.getgrnam(ACCOUNT)
    record = Path(f"/etc/containers/systemd/users/{account.pw_uid}/.foundation-account.json")
    regular(record, owner=0)
    declaration = json.loads(record.read_text())
    if (declaration.get("name") != ACCOUNT or declaration.get("uid") != account.pw_uid
            or declaration.get("gid") != group.gr_gid or account.pw_gid != group.gr_gid
            or account.pw_dir != "/var/lib/svc-forgejo"
            or account.pw_shell not in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false")):
        raise ValueError("effective foundation identity differs from its declaration")
    for kind in ("uid", "gid"):
        entries = []
        for line in Path(f"/etc/sub{kind}").read_text().splitlines():
            pieces = line.split(":")
            if len(pieces) == 3 and pieces[0] in (ACCOUNT, str(account.pw_uid)):
                entries.append((int(pieces[1]), int(pieces[2])))
        if entries != [(declaration[f"sub{kind}_start"], declaration[f"sub{kind}_count"])]:
            raise ValueError("effective subordinate mapping differs from the foundation")
    return declaration


def user_command(arguments: list[str], allocation: dict, **kwargs):
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
        "HOME": "/var/lib/svc-forgejo", "USER": ACCOUNT, "LOGNAME": ACCOUNT,
        "XDG_RUNTIME_DIR": f"/run/user/{allocation['uid']}"}
    environment.update(kwargs.pop("extra_environment", {}))
    return execute(["/usr/sbin/runuser", "-u", ACCOUNT, "--", *arguments],
                   environment=environment, **kwargs)


def manager(arguments: list[str], **kwargs):
    return execute(["/usr/bin/systemctl", "--user", "--machine=svc-forgejo@.host", *arguments], **kwargs)


def wait_health(port: int, timeout: int):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/healthz", timeout=5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise RuntimeError("Forgejo HTTP readiness deadline exceeded")


def require_port_available(port: int):
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError("declared Forgejo loopback backend port is occupied") from None


def targets_for(allocation: dict) -> dict[str, Path]:
    roots = {
        "config": STATE / "config", "credentials": STATE / "credentials",
        "quadlets": Path(f"/etc/containers/systemd/users/{allocation['uid']}"),
        "units": Path("/var/lib/svc-forgejo/.config/systemd/user"),
    }
    return {name: roots[name.split('/')[0]] / name.split('/', 1)[1]
            for name in REQUIRED_FILES if name != "manifest.json"}


def check_boundaries(allocation: dict):
    for directory in (STATE, STATE / "config", STATE / "credentials", STATE / "data",
                      STATE / "database", STATE / "backups"):
        item = directory.lstat()
        if (not stat.S_ISDIR(item.st_mode) or item.st_uid != allocation["uid"]
                or item.st_gid != allocation["gid"] or stat.S_IMODE(item.st_mode) != 0o700):
            raise ValueError("active service directory has unsafe metadata")
    for directory in (HELPERS, HELPERS / "candidates", LOCK.parent):
        item = directory.lstat()
        if not stat.S_ISDIR(item.st_mode) or item.st_uid != 0 or item.st_mode & 0o022:
            raise ValueError("administrator-owned lifecycle boundary is unsafe")
    check_lock_metadata(LOCK, allocation["gid"])


def check_lock_metadata(path: Path, group: int):
    item = path.lstat()
    if (not stat.S_ISREG(item.st_mode) or item.st_uid != os.geteuid()
            or item.st_gid != group or stat.S_IMODE(item.st_mode) != 0o660):
        raise ValueError("operation lock metadata differs from its declaration")


def validate_generator(candidate: Path, allocation: dict):
    environment = {"PATH": "/usr/bin:/bin", "QUADLET_UNIT_DIRS": str(candidate / "quadlets"),
                   "XDG_RUNTIME_DIR": f"/run/user/{allocation['uid']}"}
    execute(["/usr/lib/systemd/system-generators/podman-system-generator",
             "--user", "--dryrun"], environment=environment)


def database_command(allocation: dict, sql: str, *, user: str = "postgres",
                     database: str = "postgres") -> str:
    return user_command(["/usr/bin/podman", "exec", "-i", "forgejo-postgres",
        "psql", "-X", "-U", user, "-d", database, "--set", "ON_ERROR_STOP=1",
        "--tuples-only", "--no-align"], allocation, input_text=sql).stdout.strip()


def initialize_database(allocation: dict):
    password_file = STATE / "config/database-password"
    regular(password_file, private=True, owner=allocation["uid"])
    exists = database_command(allocation, "SELECT count(*) FROM pg_roles WHERE rolname='forgejo';")
    if exists == "0":
        password = safe_read(password_file, private=True, owner=allocation["uid"]).decode().replace("'", "''")
        database_command(allocation, f"CREATE ROLE forgejo LOGIN PASSWORD '{password}' "
                         "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;")
    flags = database_command(allocation,
        "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname='forgejo';")
    if flags != "f|f|f|f":
        raise ValueError("existing application database role has incompatible authority")
    exists = database_command(allocation, "SELECT count(*) FROM pg_database WHERE datname='forgejo';")
    if exists == "0":
        database_command(allocation, "CREATE DATABASE forgejo OWNER forgejo;")
    if database_command(allocation, "SELECT pg_get_userbyid(datdba) FROM pg_database "
            "WHERE datname='forgejo';") != "forgejo":
        raise ValueError("application database has incompatible ownership")


def admin_request(port: int, username: str, password: str, *, method="GET", payload=None):
    authorization = base64.b64encode(f"{username}:{password}".encode()).decode()
    endpoint = "/user" if method == "GET" else f"/admin/users/{username}"
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1{endpoint}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Basic " + authorization, "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        if method == "GET" and error.code == 401:
            return None
        raise RuntimeError(f"administrator initialization returned HTTP {error.code}") from None


def initialize_administrator(settings: dict, allocation: dict):
    marker = HELPERS / ".admin-initialized"
    if marker.exists():
        regular(marker, private=True, owner=os.geteuid())
        return
    inputs = STATE / "credentials/admin.json"
    regular(inputs, private=True, owner=allocation["uid"])
    admin = json.loads(safe_read(inputs, private=True, owner=allocation["uid"]).decode())
    username = admin["username"]
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", username) is None:
        raise ValueError("invalid initial administrator name")
    pending = HELPERS / ".admin-bootstrap.json"
    cli = ["/usr/bin/podman", "exec", "forgejo", "forgejo", "--config", "/etc/gitea/app.ini"]
    if pending.exists():
        regular(pending, private=True, owner=os.geteuid())
        saved = json.loads(safe_read(pending, private=True, owner=os.geteuid()).decode())
        if saved["username"] != username:
            raise ValueError("pending administrator identity needs operator recovery")
        initial = saved["password"]
    else:
        users = user_command([*cli, "admin", "user", "list"], allocation).stdout
        if any(len(line.split()) > 1 and line.split()[1] == username for line in users.splitlines()):
            raise ValueError("existing initial administrator lacks completion evidence; recover separately")
        output = user_command([*cli, "admin", "user", "create", "--username", username,
            "--email", admin["email"], "--admin", "--random-password",
            "--random-password-length", "32", "--must-change-password=false"], allocation).stdout
        matched = re.search(r"^generated random password is '([^\r\n]+)'$", output, re.M)
        if matched is None:
            raise RuntimeError("initial administrator credential was not returned")
        initial = matched.group(1)
        atomic_write(pending, json.dumps({"username": username, "password": initial}).encode(),
                     mode=0o600, uid=os.geteuid(), gid=os.getegid())
    current = admin_request(settings["backend_port"], username, admin["password"])
    if current is None:
        admin_request(settings["backend_port"], username, initial, method="PATCH",
            payload={"email": admin["email"], "password": admin["password"],
                     "must_change_password": False})
        current = admin_request(settings["backend_port"], username, admin["password"])
    if not current or not current.get("is_admin"):
        raise RuntimeError("initial administrator authentication failed")
    atomic_write(marker, b"managed\n", mode=0o600, uid=os.geteuid(), gid=os.getegid())
    pending.unlink()


def reconcile(candidate: Path) -> dict:
    if not candidate.is_absolute() or candidate.parent != HELPERS / "candidates":
        raise ValueError("candidate is outside the fixed administrator staging boundary")
    allocation = account_allocation()
    check_boundaries(allocation)
    with operation_lock(LOCK, 300):
        allocation = account_allocation()
        check_boundaries(allocation)
        if (STATE / "restart-intent.json").exists() or (STATE / "recovery-failed").exists():
            raise RuntimeError("unfinished backup recovery must complete before reconciliation")
        settings = validate_candidate(candidate, allocation)
        targets = targets_for(allocation)
        check_stable_inputs(candidate, targets)
        check_database(STATE / "database")
        stored = HELPERS / "desired.json"
        if stored.exists():
            regular(stored, owner=os.geteuid())
            previous = json.loads(safe_read(stored).decode())
            if any(previous[key] != settings[key] for key in ("image", "postgres_image")):
                raise ValueError("runtime upgrade requires a reviewed compatible pre-upgrade archive")
            generation = previous["recovery_generation"]
        else:
            if any((STATE / "database").iterdir()) or any((STATE / "data").iterdir()):
                raise ValueError("existing unrecorded data requires operator migration")
            generation = uuid.uuid4().hex
        app_active = manager(["is-active", "forgejo.service"], check=False).returncode == 0
        if not app_active:
            require_port_available(settings["backend_port"])
        validate_generator(candidate, allocation)
        changed_names = {name for name, path in targets.items()
            if not path.exists() or safe_read(path) != safe_read(candidate / name)}
        changed = publish_files(candidate, targets, allocation)
        settings["recovery_generation"] = generation
        settings["checksums"] = {name: hashlib.sha256(safe_read(path)).hexdigest()
                                 for name, path in targets.items()}
        desired_bytes = (json.dumps(settings, sort_keys=True, indent=2) + "\n").encode()
        if not stored.exists() or safe_read(stored) != desired_bytes:
            atomic_write(stored, desired_bytes, mode=0o640, uid=os.geteuid(), gid=allocation["gid"])
            changed = True
        for key in ("postgres_image", "image", "rclone_image"):
            exists = user_command(["/usr/bin/podman", "image", "exists", settings[key]],
                                  allocation, check=False)
            if exists.returncode != 0:
                user_command(["/usr/bin/podman", "pull", settings[key]], allocation, timeout=600)
                changed = True
        if changed:
            manager(["daemon-reload"])
        database_active = manager(["is-active", "forgejo-postgres.service"], check=False).returncode == 0
        database_changed = bool(changed_names & {"quadlets/forgejo-postgres.container",
            "quadlets/forgejo.network", "credentials/postgres.env"})
        if not database_active or database_changed:
            manager(["restart" if database_active else "start", "forgejo-postgres.service"])
            changed = True
        deadline = time.monotonic() + settings["health_timeout_seconds"]
        while user_command(["/usr/bin/podman", "exec", "forgejo-postgres", "pg_isready",
                            "-U", "postgres"], allocation, check=False).returncode:
            if time.monotonic() >= deadline:
                raise RuntimeError("PostgreSQL readiness deadline exceeded")
            time.sleep(1)
        initialize_database(allocation)
        app_changed = database_changed or any(name.startswith("config/") for name in changed_names) or "quadlets/forgejo.container" in changed_names
        if not app_active or app_changed:
            if not app_active:
                require_port_available(settings["backend_port"])
            manager(["restart" if app_active else "start", "forgejo.service"])
            changed = True
        wait_health(settings["backend_port"], settings["health_timeout_seconds"])
        initialize_administrator(settings, allocation)
        result = verify()
        result["changed"] = changed
        return result


def verify() -> dict:
    stored = HELPERS / "desired.json"
    regular(stored, owner=os.geteuid())
    settings = json.loads(safe_read(stored).decode())
    allocation = account_allocation()
    if settings["allocation"] != allocation:
        raise ValueError("existing deployment allocation differs from foundation state")
    check_boundaries(allocation)
    check_database(STATE / "database")
    for name, path in targets_for(allocation).items():
        protected = name.startswith(("config/", "credentials/"))
        regular(path, private=protected, owner=allocation["uid"] if protected else os.geteuid())
        if path.stat().st_gid != allocation["gid"]:
            raise ValueError("deployment file has unexpected group")
        if hashlib.sha256(safe_read(path)).hexdigest() != settings["checksums"][name]:
            raise ValueError("effective declaration differs from installed desired state")
    for container, key in (("forgejo", "image"), ("forgejo-postgres", "postgres_image")):
        manager(["is-active", container + ".service"])
        expected = user_command(["/usr/bin/podman", "image", "inspect", "--format",
                                 "{{.Id}}", settings[key]], allocation).stdout.strip()
        actual = user_command(["/usr/bin/podman", "container", "inspect", "--format",
                               "{{.Image}}", container], allocation).stdout.strip()
        if expected != actual:
            raise ValueError("running container does not use the declared immutable image")
        document = json.loads(user_command(["/usr/bin/podman", "container", "inspect",
                                           container], allocation).stdout)[0]
        ports = document["NetworkSettings"].get("Ports") or {}
        published = {key: value for key, value in ports.items() if value}
        expected_ports = {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(settings["backend_port"])}]}
        if published != (expected_ports if container == "forgejo" else {}):
            raise ValueError("container exposes an undeclared host listener")
    if database_command(allocation, "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication "
            "FROM pg_roles WHERE rolname='forgejo';", user="forgejo", database="forgejo") != "f|f|f|f":
        raise ValueError("application database role has excessive authority")
    wait_health(settings["backend_port"], 5)
    return {"ready": True, "backup": "pending", "mirror": "pending",
            "acceptance_complete": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Internal managed Forgejo lifecycle")
    actions = parser.add_subparsers(dest="action", required=True)
    provision = actions.add_parser("reconcile")
    provision.add_argument("--candidate", required=True, type=Path)
    actions.add_parser("verify")
    options = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise ValueError("managed host lifecycle requires authorized root administration")
        if options.action == "reconcile":
            result = reconcile(options.candidate)
        else:
            result = verify()
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as error:
        print(f"Forgejo lifecycle failed: {error}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
