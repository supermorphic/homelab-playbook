"""Read-only Debian fixture observations; executed via pinned SSH, never installed."""

import grp
import json
import os
from pathlib import Path
import platform
import pwd
import shutil
import stat


def secure_path(path):
    """Every existing path component must be root-owned and not replaceable by workers."""
    for parent in (path, *path.parents):
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            return False
    return True


def subordinate_ranges(path):
    allocations = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        user, start, count = line.split(":")
        allocations.append({"user": user, "start": int(start), "count": int(count)})
    return allocations


def observe(allocation: dict) -> dict:
    """Gather metadata only; do not open runtime sockets or protected inventory."""
    root = Path(allocation["state_root"])
    marker_path = root.parent / "ownership"
    metadata = marker_path.lstat() if os.path.lexists(marker_path) else None
    parent_exists = os.path.lexists(root.parent)
    parent_secure = secure_path(root.parent if parent_exists else root.parent.parent)
    regular = metadata is not None and stat.S_ISREG(metadata.st_mode)
    # Do not follow a replaceable/symlink marker or read a device/FIFO.
    marker = marker_path.read_text().strip() if parent_secure and regular and metadata.st_uid == 0 and not metadata.st_mode & 0o022 else None
    os_release = platform.freedesktop_os_release()
    userns_path = Path("/proc/sys/kernel/unprivileged_userns_clone")
    userns = int(userns_path.read_text()) > 0 if userns_path.exists() else True
    userns = userns and int(Path("/proc/sys/user/max_user_namespaces").read_text()) > 0
    return {
        "os_id": os_release.get("ID"), "os_version": os_release.get("VERSION_ID"),
        "architecture": platform.machine(), "effective_uid": os.geteuid(),
        "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
        "user_namespaces": userns,
        "marker": {"value": marker, "uid": metadata.st_uid if metadata else None,
                   "mode": stat.S_IMODE(metadata.st_mode) if metadata else None, "regular": regular},
        "state_parent_exists": parent_exists,
        "state_root_exists": os.path.lexists(root), "state_parent_secure": parent_secure,
        "users": [{"user": user.pw_name, "uid": user.pw_uid, "gid": user.pw_gid} for user in pwd.getpwall()],
        "groups": [{"group": group.gr_name, "gid": group.gr_gid} for group in grp.getgrall()],
        "subuids": subordinate_ranges(Path("/etc/subuid")),
        "subgids": subordinate_ranges(Path("/etc/subgid")),
        "commands": {command: shutil.which(command) is not None for command in (
            "podman", "systemctl", "systemd-run", "ip", "nft", "mount", "umount", "newuidmap", "newgidmap")},
    }
