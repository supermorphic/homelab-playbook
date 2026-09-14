"""Bounded, secret-free observations of the existing Molecule lifecycle."""

from __future__ import annotations

import json
import math
import re
import subprocess
import time
from pathlib import Path

EVENT_PREFIX = "HOMELAB_TIMING "
PHASES = (
    "create",
    "prepare",
    "converge",
    "idempotence",
    "verify",
    "cleanup",
    "destroy",
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SAFE_PATH = re.compile(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+(?::[0-9]+)?\Z")


def provenance(root: Path) -> dict:
    """Read only revision and dirty status; never retain changed filenames."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ValueError("invalid revision")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"commit": "unavailable", "worktree": "unknown"}
    return {"commit": commit, "worktree": "uncommitted changes" if status else "clean"}


class TimingCollector:
    def __init__(self, scenario: str, *, max_tasks: int = 4096):
        self.marker = re.compile(
            rf"^(?:INFO|WARNING|ERROR|CRITICAL)\s+(?:"
            rf"\[{re.escape(scenario)} > (?P<plain>[a-z_]+)\]|"
            rf"{re.escape(scenario)} ➜ (?P<color>[a-z_]+):) "
            r"(?P<event>Executing|Executed: [^\r\n]+)$"
        )
        self.max_tasks = max_tasks
        self.phases: list[dict] = []
        self.active: dict | None = None
        self.tasks: dict[tuple, dict] = {}
        self.roles: dict[tuple, dict] = {}
        self.role_runs: set[tuple] = set()
        self.limited = False
        self.invalid_events = 0
        self.task_events = 0

    def observe(self, line: str, *, at: float | None = None) -> bool:
        """Return true only for internal records that should not be printed."""
        line = ANSI.sub("", line).strip()
        if line.startswith(EVENT_PREFIX):
            self._task(line[len(EVENT_PREFIX) :])
            return True
        match = self.marker.match(line)
        if match is None:
            return False
        phase = match["plain"] or match["color"]
        at = time.monotonic() if at is None else at
        if match["event"] == "Executing":
            self.finish(at)
            if len(self.phases) >= 64:
                self.limited = True
                return False
            self.active = {
                "phase": phase,
                "started": at,
                "seconds": 0.0,
                "status": "incomplete",
                "execution": 1 + sum(p["phase"] == phase for p in self.phases),
            }
            self.phases.append(self.active)
        elif self.active is not None and self.active["phase"] == phase:
            outcome = match["event"].removeprefix("Executed: ").lower()
            known = {
                "successful",
                "failed",
                "partial",
                "skipped",
                "missing",
                "disabled",
            }
            first = outcome.split()[0]
            states = set(
                re.findall(
                    r"\d+ (successful|failed|partial|skipped|missing|disabled)\b",
                    outcome,
                )
            )
            if first in known:
                status = first
            elif "failed" in states:
                status = "failed"
            elif len(states) == 1:
                status = next(iter(states))
            else:
                status = "partial" if states else "completed"
            self.active["status"] = status
            self.finish(at)
        return False

    def finish(self, at: float):
        if self.active is not None:
            self.active["seconds"] = max(0.0, at - self.active["started"])
            self.active = None

    def _task(self, payload: str):
        try:
            if len(payload) > 2048:
                raise ValueError("oversized timing event")
            event = json.loads(payload)
            if set(event) != {"source", "role", "role_run", "seconds"}:
                raise ValueError("unexpected timing fields")
            for field in ("source", "role"):
                value = event[field]
                if (
                    not isinstance(value, str)
                    or len(value) > 512
                    or not (
                        value in {"<generated>", "<none>"}
                        or (SAFE_PATH.fullmatch(value) and ".." not in value.split("/"))
                    )
                ):
                    raise ValueError("invalid source identifier")
            seconds = event["seconds"]
            if (
                type(seconds) not in (int, float)
                or not math.isfinite(seconds)
                or seconds < 0
                or seconds > 86400
            ):
                raise ValueError("invalid duration")
            if not isinstance(event["role_run"], str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", event["role_run"]
            ):
                raise ValueError("invalid role execution identifier")
        except (ValueError, TypeError, KeyError):
            self.invalid_events += 1
            return
        self.task_events += 1
        phase = self.active["phase"] if self.active else "unattributed"
        execution = self.active["execution"] if self.active else 0
        key = (phase, execution, event["source"])
        if key not in self.tasks and len(self.tasks) >= self.max_tasks:
            self.limited = True
            return
        task = self.tasks.setdefault(
            key,
            {
                "phase": phase,
                "execution": execution,
                "source": event["source"],
                "seconds": 0.0,
                "count": 0,
                "max_seconds": 0.0,
            },
        )
        task["seconds"] += seconds
        task["count"] += 1
        task["max_seconds"] = max(task["max_seconds"], seconds)
        role_key = (phase, execution, event["role"])
        if role_key not in self.roles and len(self.roles) >= self.max_tasks:
            self.limited = True
            return
        role = self.roles.setdefault(
            role_key,
            {
                "phase": phase,
                "execution": execution,
                "role": event["role"],
                "seconds": 0.0,
                "tasks": 0,
                "executions": 0,
            },
        )
        role["seconds"] += seconds
        role["tasks"] += 1
        run_key = (*role_key, event["role_run"])
        if run_key not in self.role_runs:
            if len(self.role_runs) >= self.max_tasks:
                self.limited = True
            else:
                self.role_runs.add(run_key)
                role["executions"] += 1

    def report(self) -> dict:
        return {
            "phases": [
                {k: v for k, v in p.items() if k != "started"} for p in self.phases
            ],
            "not_run": [
                phase
                for phase in PHASES
                if not any(p["phase"] == phase for p in self.phases)
            ],
            "tasks": sorted(self.tasks.values(), key=lambda t: -t["seconds"])[:20],
            "roles": sorted(self.roles.values(), key=lambda r: -r["seconds"])[:20],
            "task_events": self.task_events,
            "limited": self.limited,
            "invalid_events": self.invalid_events,
        }


def render(report: dict) -> str:
    """The same bounded Markdown tables work in the terminal and job summary."""
    lines = [
        "",
        "| Phase | Execution | Seconds | Result |",
        "| --- | ---: | ---: | --- |",
    ]
    for phase in report["phases"]:
        lines.append(
            f"| {phase['phase']} | {phase['execution']} | "
            f"{phase['seconds']:.2f} | {phase['status']} |"
        )
    for phase in report["not_run"]:
        lines.append(f"| {phase} | — | — | not run |")
    lines += [
        "",
        "Task elapsed time includes controller/connection overhead; "
        "the last task of an interrupted playbook may be unavailable.",
        f"Task events: {report['task_events']}; limited: {report['limited']}; "
        f"invalid events: {report['invalid_events']}.",
        "",
        "| Phase / execution | Task source (slowest 20 totals) | Calls | Seconds | Max |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for task in report["tasks"]:
        lines.append(
            f"| {task['phase']} / {task['execution']} | `{task['source']}` | "
            f"{task['count']} | {task['seconds']:.2f} | {task['max_seconds']:.2f} |"
        )
    lines += [
        "",
        "| Phase / execution | Role (slowest 20 totals) | Executions | Tasks | Seconds |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for role in report["roles"]:
        lines.append(
            f"| {role['phase']} / {role['execution']} | `{role['role']}` | "
            f"{role['executions']} | {role['tasks']} | {role['seconds']:.2f} |"
        )
    if not report["task_events"]:
        lines += ["", "Task timing unavailable: no callback records received."]
    return "\n".join(lines) + "\n"
