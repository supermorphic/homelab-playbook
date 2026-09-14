"""Task timing for disposable Molecule tests; never inspect task values/results."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = r"""
name: homelab_timing
type: aggregate
short_description: Bounded source-only task timing for repository Molecule tests
description:
  - Enabled only by the repository Molecule runner.
  - Emits durations and source identifiers without task names or values.
requirements:
  - Enable in the callback whitelist.
"""


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "homelab_timing"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super().__init__()
        self.root = Path(os.environ["HOMELAB_TIMING_ROOT"]).resolve()
        self.roots = [
            (self.root / prefix, prefix)
            for prefix in (
                ".ansible/roles",
                ".ansible/collections",
                "roles",
                "playbooks",
            )
        ]
        self.current = None
        self.started = 0.0
        self.invocation = uuid.uuid4().hex

    def _source(self, value):
        # Resolve the approved source roots, including Galaxy symlinks. No file
        # contents, task names, arguments, hostnames, or result objects are read.
        path, separator, line = str(value).rpartition(":")
        if not separator or not line.isdecimal():
            path, line = str(value), ""
        try:
            resolved = Path(path).resolve()
            for root, prefix in self.roots:
                if resolved.is_relative_to(root.resolve()):
                    relative = (
                        f"{prefix}/{resolved.relative_to(root.resolve())}".rstrip("/.")
                    )
                    if len(relative) <= 480 and re.fullmatch(
                        r"[A-Za-z0-9_./-]+", relative
                    ):
                        return relative + (f":{line}" if line else "")
        except (OSError, ValueError):
            pass
        return "<generated>"

    def _finish(self):
        if self.current is not None:
            event = dict(
                self.current, seconds=max(0.0, time.monotonic() - self.started)
            )
            self._display.display(
                "HOMELAB_TIMING " + json.dumps(event, separators=(",", ":"))
            )
            self.current = None

    def _start(self, task):
        self._finish()
        role = task._role
        self.current = {
            "source": self._source(task.get_path()),
            "role": self._source(role.get_role_path()) if role else "<none>",
            # Ansible UUIDs can encode the controller node identity. Salt and
            # hash the value; never transmit that internal identifier.
            "role_run": hashlib.sha256(
                (self.invocation + "_" + (str(role._uuid) if role else "none")).encode()
            ).hexdigest(),
        }
        self.started = time.monotonic()

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._start(task)

    def v2_playbook_on_handler_task_start(self, task):
        self._start(task)

    def v2_playbook_on_stats(self, stats):
        self._finish()
