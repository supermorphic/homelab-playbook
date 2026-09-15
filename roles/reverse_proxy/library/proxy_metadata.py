#!/usr/bin/python
"""Observe the declared proxy precondition paths in one remote module call."""

from __future__ import annotations

DOCUMENTATION = r"""
module: proxy_metadata
short_description: Read filesystem metadata for reverse proxy preconditions
description:
  - Observes each declared path without following its final symlink.
  - Returns only metadata consumed by the reverse proxy filesystem assertions.
  - Missing paths return C(exists=false); other lookup errors stop the batch
    and identify the failing path.
  - Does not read file contents or cache observations between calls.
options:
  paths:
    description: Explicit paths to observe, in assertion order.
    type: list
    elements: path
    required: true
attributes:
  check_mode:
    support: full
  diff_mode:
    support: none
author:
  - Homelab Playbook contributors
"""

EXAMPLES = r"""
- name: Inspect reverse proxy parent directories
  proxy_metadata:
    paths:
      - /etc
      - /etc/caddy
  register: reverse_proxy_parent_directories
"""

RETURN = r"""
failed_path:
  description: Path whose observation failed; no partial batch is accepted.
  returned: failure
  type: str
results:
  description: Ordered items with the path and its stat-compatible metadata.
  returned: success
  type: list
  elements: dict
  contains:
    item:
      description: Observed path.
      type: str
    stat:
      description: Existence, type, numeric and named ownership, mode, and link count.
      type: dict
"""

import grp
import os
import pwd
import stat

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.common.text.converters import to_bytes


def observe(path):
    """Match the consumed subset of ansible.builtin.stat with follow=false."""
    try:
        metadata = os.lstat(to_bytes(path, errors="surrogate_or_strict"))
    except FileNotFoundError:
        return {"exists": False}

    result = {
        "exists": True,
        "isdir": stat.S_ISDIR(metadata.st_mode),
        "isreg": stat.S_ISREG(metadata.st_mode),
        "islnk": stat.S_ISLNK(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "mode": "%04o" % stat.S_IMODE(metadata.st_mode),
        "nlink": metadata.st_nlink,
    }
    try:
        result["pw_name"] = pwd.getpwuid(metadata.st_uid).pw_name
    except (TypeError, KeyError):
        pass
    try:
        result["gr_name"] = grp.getgrgid(metadata.st_gid).gr_name
    except (KeyError, ValueError, OverflowError):
        pass
    return result


def main():
    module = AnsibleModule(
        argument_spec={"paths": {"type": "list", "elements": "path", "required": True}},
        supports_check_mode=True,
    )
    results = []
    for path in module.params["paths"]:
        try:
            results.append({"item": path, "stat": observe(path)})
        except OSError as error:
            module.fail_json(msg=error.strerror, failed_path=path, exception=error)
    module.exit_json(changed=False, results=results)


if __name__ == "__main__":
    main()
