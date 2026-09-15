"""Create synthetic filesystem cases inside the disposable proxy container."""

import grp
import json
import os
from pathlib import Path
import pwd
import socket
import sys


root = Path(sys.argv[1])
root.mkdir(mode=0o755)
cases = []


def case(name, kind, mode, owner=0, group=0):
    path = root / name
    if kind == "directory":
        path.mkdir()
    elif kind == "file":
        path.write_text("synthetic metadata fixture\n")
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        with socket.socket(socket.AF_UNIX) as stream:
            stream.bind(str(path))
    elif kind == "symlink":
        path.symlink_to(root / "file")
    elif kind == "dangling":
        path.symlink_to(root / "missing")
    if kind not in ("missing", "symlink", "dangling"):
        os.chown(path, owner, group)
        path.chmod(mode)
    return str(path)


caddy = pwd.getpwnam("caddy")
# Fit standard rootless mappings and prove that named ownership is absent.
unknown_identity = 60000
for lookup in (pwd.getpwuid, grp.getgrgid):
    try:
        lookup(unknown_identity)
    except KeyError:
        pass
    else:
        raise RuntimeError("The synthetic unnamed fixture identity is already assigned")
paths = {
    "directory": case("directory", "directory", 0o755),
    "file": case("file", "file", 0o640),
    "missing": case("missing", "missing", 0),
    "writable": case("writable", "directory", 0o777),
    "sticky": case("sticky", "directory", 0o1777),
    "foreign": case("foreign", "directory", 0o755, caddy.pw_uid, caddy.pw_gid),
    "unknown": case("unknown", "directory", 0o755, unknown_identity, unknown_identity),
    "wrong-group": case("wrong-group", "directory", 0o755, caddy.pw_uid, 0),
    "writable-file": case("writable-file", "file", 0o666),
    "foreign-file": case("foreign-file", "file", 0o600, caddy.pw_uid, caddy.pw_gid),
    "fifo": case("fifo", "fifo", 0o600),
    "socket": case("socket", "socket", 0o600),
    "symlink": case("symlink", "symlink", 0),
    "dangling": case("dangling", "dangling", 0),
}
os.lchown(paths["symlink"], caddy.pw_uid, caddy.pw_gid)
paths["hardlink"] = case("hardlink", "file", 0o600)
os.link(paths["hardlink"], root / "second-link")

# Expected decisions are hand specified independently of both observers.
for name, parent, directory, file in (
    ("directory", True, True, False),
    ("file", False, False, True),
    ("missing", False, True, True),
    ("writable", False, False, False),
    ("sticky", False, False, False),
    ("foreign", False, False, False),
    ("unknown", False, False, False),
    ("wrong-group", False, False, False),
    ("writable-file", False, False, False),
    ("foreign-file", False, False, False),
    ("fifo", False, False, False),
    ("socket", False, False, False),
    ("symlink", False, False, False),
    ("dangling", False, False, False),
    ("hardlink", False, False, False),
):
    cases.append({"path": paths[name], "logical": "/etc/caddy", "want": [parent, directory, file]})
cases.extend([
    {"path": paths["sticky"], "logical": "/run/lock", "want": [True, False, False]},
    {"path": paths["foreign"], "logical": "/run/caddy", "want": [False, True, False]},
    {"path": paths["wrong-group"], "logical": "/run/caddy", "want": [False, False, False]},
    {"path": paths["directory"], "logical": "/var/lib/caddy", "want": [True, False, False]},
])

# ENOTDIR, ELOOP and EACCES must fail; only ENOENT means missing.
denied = root / "denied"
denied.mkdir(mode=0o700)
(denied / "child").touch()
(root / "loop").symlink_to(root / "loop")
print(json.dumps({"cases": cases, "errors": [str(root / "file/child"),
      str(root / "loop/child"), str(denied / "child")]}))
