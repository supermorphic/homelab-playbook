"""Offline safety contracts for repository Ansible sources."""

from __future__ import annotations

import configparser
import ast
from collections import Counter
from copy import deepcopy
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_yaml_documents(relative_path: str) -> list[object]:
    with (REPOSITORY_ROOT / relative_path).open(encoding="utf-8") as source:
        return list(yaml.safe_load_all(source))


def load_tasks(relative_path: str) -> list[dict[str, object]]:
    return [task for document in load_yaml_documents(relative_path) for task in document]


OBSERVATIONAL_VERIFIER_MODULES = {
    "ansible.builtin.assert", "ansible.builtin.command", "ansible.builtin.debug",
    "ansible.builtin.getent", "ansible.builtin.import_tasks",
    "ansible.builtin.package_facts", "ansible.builtin.script", "ansible.builtin.set_fact",
    "ansible.builtin.slurp", "ansible.builtin.stat",
}

TASK_META_KEYS = {
    "name", "when", "register", "changed_when", "failed_when", "no_log",
    "become", "become_user", "become_flags", "loop", "loop_control", "vars",
    "tags", "delegate_to", "delegate_facts", "environment", "run_once",
    "check_mode", "ignore_errors", "any_errors_fatal", "throttle", "timeout",
    "until", "retries", "delay", "notify", "listen", "args",
}

READ_ONLY_ARGV = {
    ("sudo", "-n", "true"),
    ("/usr/sbin/sshd", "-T"),
    ("/usr/sbin/getenforce",),
    ("/usr/sbin/aa-status", "--enabled"),
    ("/usr/sbin/aa-status", "--json"),
    ("/usr/bin/chronyc", "tracking"),
    ("/usr/bin/chronyc", "sources"),
    ("/usr/bin/apt-config", "dump"),
    ("/usr/bin/dpkg", "--audit"),
    ("/usr/bin/dpkg-query", "-L", "apparmor", "apparmor-profiles"),
    ("/usr/bin/dnf", "check"),
    ("/usr/bin/dnf", "needs-restarting", "-r"),
    ("/usr/bin/systemd-analyze", "cat-config", "systemd/journald.conf"),
    ("/usr/bin/systemctl", "is-enabled", "systemd-timesyncd.service"),
    ("/usr/bin/systemctl", "is-active", "systemd-timesyncd.service"),
    ("/usr/bin/timedatectl", "show", "--property=NTPSynchronized", "--value"),
    ("/usr/bin/systemctl", "is-enabled", "chronyd.service"),
    ("/usr/bin/systemctl", "is-active", "chronyd.service"),
    ("/usr/bin/systemctl", "is-active", "auditd"),
    ("/usr/bin/systemctl", "--failed", "--no-legend", "--plain"),
}

READ_ONLY_ARGV_TEMPLATES = {
    "{{ ['/usr/bin/firewall-offline-cmd', '--zone=homelab', item] }}",
    "{{ ['/usr/bin/firewall-cmd', '--zone=homelab', item] }}",
    "{{ ['/usr/bin/firewall-cmd', item] }}",
    "{{ ['/usr/bin/systemctl', 'is-enabled',\n    {{ 'apt-daily-upgrade.timer' if ansible_os_family == 'Debian'\n       else 'dnf-automatic.timer' }}] }}",
    "{{ ['/usr/bin/systemd-analyze', 'cat-config',\n    {{ 'systemd/system/apt-daily-upgrade.timer'\n       if ansible_os_family == 'Debian'\n       else 'systemd/system/dnf-automatic.timer' }}] }}",
}

READ_ONLY_ARGV_DYNAMIC_LISTS = {
    (
        "/usr/bin/systemctl", "is-enabled",
        "{{ 'apt-daily-upgrade.timer' if ansible_os_family == 'Debian'\n   else 'dnf-automatic.timer' }}",
    ),
    (
        "/usr/bin/systemd-analyze", "cat-config",
        "{{ 'systemd/system/apt-daily-upgrade.timer'\n   if ansible_os_family == 'Debian'\n   else 'systemd/system/dnf-automatic.timer' }}",
    ),
}

FIREWALL_ZONE_READS = [
    "--list-all", "--list-interfaces", "--list-sources", "--list-services",
    "--list-ports", "--list-protocols", "--list-source-ports",
    "--list-forward-ports", "--list-rich-rules", "--query-forward",
    "--query-masquerade",
]
FIREWALL_TEMPLATE_LOOPS = {
    "{{ ['/usr/bin/firewall-offline-cmd', '--zone=homelab', item] }}": FIREWALL_ZONE_READS,
    "{{ ['/usr/bin/firewall-cmd', '--zone=homelab', item] }}": FIREWALL_ZONE_READS,
    "{{ ['/usr/bin/firewall-cmd', item] }}": [
        "--get-default-zone",
        "--get-zone-of-interface={{ os_baseline_verify_management.interface }}",
        "--get-active-zones",
    ],
}


def assert_observational_script(path: Path) -> None:
    if not path.is_file():
        raise AssertionError(f"verifier script is missing: {path}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    contracts = {
        "discover_management_interface.py": {"ipaddress", "json", "os", "pathlib", "subprocess"},
    }
    if path.name not in contracts:
        raise AssertionError(f"verifier script is not allowlisted: {path.name}")
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import)]
    if any(alias.asname for node in imports for alias in node.names):
        raise AssertionError("verifier script import aliases are not allowed")
    imported = {alias.name for node in imports for alias in node.names}
    if imported != contracts[path.name]:
        raise AssertionError(f"verifier script imports are not exact: {imported}")
    from_imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    if len(from_imports) != 1 or from_imports[0].module != "__future__" or [alias.name for alias in from_imports[0].names] != ["annotations"]:
        raise AssertionError("verifier script direct callable imports are not allowed")
    allowed_calls = {
        "os.getpid", "connection.split", "str", "subprocess.run", "json.loads", "print",
        "len", "ValueError", "ipaddress.ip_address", "json.dumps",
        "pathlib.Path(f'/proc/{process}/environ').read_bytes().split", "int", "isinstance",
        "discover", "entry.startswith", "routes[0].get",
        "pathlib.Path(f'/proc/{process}/environ').read_bytes", "entry.split(b'=', 1)[1].decode",
        "pathlib.Path(f'/proc/{process}/stat').read_text().rsplit(') ', 1)[1].split",
        "pathlib.Path", "entry.split", "pathlib.Path(f'/proc/{process}/stat').read_text().rsplit",
        "pathlib.Path(f'/proc/{process}/stat').read_text",
    }
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    call_counts = Counter(ast.unparse(call.func) for call in calls)
    expected_call_counts = Counter({
        "os.getpid": 1, "connection.split": 1, "str": 1, "subprocess.run": 1,
        "json.loads": 1, "print": 1, "len": 2, "ValueError": 2,
        "ipaddress.ip_address": 1, "json.dumps": 1,
        "pathlib.Path(f'/proc/{process}/environ').read_bytes().split": 1,
        "int": 1, "isinstance": 2, "discover": 1, "entry.startswith": 1,
        "routes[0].get": 1, "pathlib.Path(f'/proc/{process}/environ').read_bytes": 1,
        "entry.split(b'=', 1)[1].decode": 1,
        "pathlib.Path(f'/proc/{process}/stat').read_text().rsplit(') ', 1)[1].split": 1,
        "pathlib.Path": 2, "entry.split": 1,
        "pathlib.Path(f'/proc/{process}/stat').read_text().rsplit": 1,
        "pathlib.Path(f'/proc/{process}/stat').read_text": 1,
    })
    if set(call_counts) - allowed_calls or call_counts != expected_call_counts:
        raise AssertionError("management script call receiver is not exact")
    subprocess_calls = [
        call for call in calls
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name) and call.func.value.id == "subprocess"
        and call.func.attr == "run"
    ]
    if len(subprocess_calls) != 1:
        raise AssertionError("verifier script must make exactly one state-read command")
    command = subprocess_calls[0].args[0] if subprocess_calls[0].args else None
    if (not isinstance(command, ast.List) or len(command.elts) != 5
            or [item.value for item in command.elts[:4] if isinstance(item, ast.Constant)] != ["/usr/sbin/ip", "-json", "route", "get"]
            or not isinstance(command.elts[4], ast.Name) or command.elts[4].id != "peer"):
        raise AssertionError("verifier script command is not the exact route-state read")


def assert_read_only_command(task: dict[str, object], command: object) -> None:
    if not isinstance(command, dict) or set(command) != {"argv"}:
        raise AssertionError("verifier command must use only list-form argv")
    argv = command["argv"]
    if isinstance(argv, str):
        normalized = "\n".join(line.strip() for line in argv.strip().splitlines())
        allowed_templates = {"\n".join(line.strip() for line in value.splitlines()) for value in READ_ONLY_ARGV_TEMPLATES}
        if normalized not in allowed_templates:
            raise AssertionError(f"command argv template is not allowlisted: {argv}")
        expected_loop = FIREWALL_TEMPLATE_LOOPS.get(normalized)
        if expected_loop is not None and task.get("loop") != expected_loop:
            raise AssertionError("templated verifier command loop is not exact")
    elif not isinstance(argv, list) or tuple(argv) not in READ_ONLY_ARGV | READ_ONLY_ARGV_DYNAMIC_LISTS:
        raise AssertionError(f"command argv is not allowlisted: {argv}")
    if task.get("changed_when") is not False:
        raise AssertionError("verifier command must declare changed_when: false")
    if task.get("failed_when") is False:
        raise AssertionError("verifier command must preserve failure semantics")


def assert_observational_verifier_task(task: dict[str, object]) -> None:
    aliases = {key.removeprefix("ansible.builtin."): key for key in OBSERVATIONAL_VERIFIER_MODULES}
    wrappers = [name for name in ("block", "rescue", "always") if name in task]
    action_keys = [key for key in task if key not in TASK_META_KEYS and key not in wrappers]
    normalized = [aliases.get(key, key) for key in action_keys]
    if wrappers:
        if normalized:
            raise AssertionError(f"wrapper has a direct action: {task.get('name')}")
        for nested in wrappers:
            children = task[nested]
            if not isinstance(children, list) or not children:
                raise AssertionError(f"empty verifier {nested} wrapper")
            for child in children:
                if not isinstance(child, dict):
                    raise AssertionError(f"invalid verifier {nested} child")
                assert_observational_verifier_task(child)
        return
    if len(normalized) != 1 or normalized[0] not in OBSERVATIONAL_VERIFIER_MODULES:
        raise AssertionError(f"non-observational verifier task: {task.get('name')}")
    command = task.get("ansible.builtin.command", task.get("command"))
    if command is not None:
        assert_read_only_command(task, command)
    script = task.get("ansible.builtin.script", task.get("script"))
    if script is not None:
        scripts = {
            ("discover_management_interface.py", "/usr/bin/python3"),
        }
        if not isinstance(script, dict) or set(script) != {"cmd", "executable"} or (script["cmd"], script["executable"]) not in scripts:
            raise AssertionError("verifier script invocation is not allowlisted")
        if task.get("changed_when") is not False:
            raise AssertionError("verifier script must declare changed_when: false")
        assert_observational_script(REPOSITORY_ROOT / "roles/os_baseline_verify/files" / str(script["cmd"]).split()[0])


COMBINED_REBOOT_EXPRESSION = """{{ (system_maintenance_reboot_required | default(false) | bool)
or (security_baseline_reboot_required | default(false) | bool) }}"""


def normalize_expression(value: object) -> str:
    if not isinstance(value, str):
        raise AssertionError(f"expected a Jinja expression, got {value!r}")
    return " ".join(value.split())


def task_module(task: dict[str, object]) -> str:
    actions = [key for key in task if key not in TASK_META_KEYS]
    if len(actions) != 1:
        raise AssertionError(f"task does not have one direct module: {task!r}")
    return actions[0]


def unique_task_index(
    tasks: list[dict[str, object]], module: str, payload: object,
) -> int:
    matches = [
        index for index, task in enumerate(tasks)
        if module in task and task[module] == payload
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one {module} task with payload {payload!r}, got {len(matches)}"
        )
    return matches[0]


def unique_module_index(tasks: list[dict[str, object]], module: str) -> int:
    matches = [
        index for index, task in enumerate(tasks)
        if task_module(task) == module
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one {module} task, got {len(matches)}")
    return matches[0]


def unique_fact_task_index(tasks: list[dict[str, object]], fact: str) -> int:
    matches = [
        index for index, task in enumerate(tasks)
        if isinstance(task.get("ansible.builtin.set_fact"), dict)
        and fact in task["ansible.builtin.set_fact"]
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one set_fact task for {fact}, got {len(matches)}")
    return matches[0]


def assert_combined_reboot_fact(task: dict[str, object]) -> None:
    facts = task.get("ansible.builtin.set_fact")
    if not isinstance(facts, dict) or set(facts) != {"os_reboot_required"}:
        raise AssertionError("reboot combination must set only os_reboot_required")
    if normalize_expression(facts["os_reboot_required"]) != normalize_expression(
        COMBINED_REBOOT_EXPRESSION
    ):
        raise AssertionError("reboot combination must OR both default-false boolean facts")


def assert_reboot_result_is_recorded(
    reboot_task: dict[str, object], record_task: dict[str, object],
) -> None:
    registered = reboot_task.get("register")
    if not isinstance(registered, str) or not registered:
        raise AssertionError("reboot task must register its result")
    facts = record_task.get("ansible.builtin.set_fact")
    if not isinstance(facts, dict) or set(facts) != {"os_reboot_performed"}:
        raise AssertionError("reboot record must set only os_reboot_performed")
    expected = f"{{{{ {registered}.changed | default(false) | bool }}}}"
    if normalize_expression(facts["os_reboot_performed"]) != expected:
        raise AssertionError("reboot record must consume the registered reboot result")


def assert_strictly_ordered(*indices: int) -> None:
    if list(indices) != sorted(indices) or len(set(indices)) != len(indices):
        raise AssertionError(f"tasks are not strictly ordered: {indices}")


def assert_scheduler_neutral_maintenance(play: dict[str, object]) -> None:
    executable_sections = {
        section
        for section in ("roles", "tasks", "pre_tasks", "post_tasks", "handlers")
        if section in play
    }
    if executable_sections != {"pre_tasks", "post_tasks"}:
        raise AssertionError(
            "maintenance may execute only its pre_tasks and post_tasks sections"
        )
    actual_modules = [task_module(task) for task in play["pre_tasks"]]
    if actual_modules != [
        "ansible.builtin.assert",
        "ansible.builtin.assert",
        "ansible.builtin.import_role",
        "ansible.builtin.import_role",
        "ansible.builtin.set_fact",
        "ansible.builtin.debug",
        "ansible.builtin.reboot",
        "ansible.builtin.set_fact",
        "ansible.builtin.meta",
        "ansible.builtin.setup",
    ]:
        raise AssertionError("maintenance executable task composition is not allowed")


class SourceContractTests(unittest.TestCase):
    def test_os_playbooks_are_sequential_and_reboot_at_playbook_level(self) -> None:
        provision, maintain = (
            load_yaml_documents(path)[0]
            for path in ("playbooks/os/provision.yml", "playbooks/os/maintain.yml")
        )
        for plays in (provision, maintain):
            self.assertTrue(all(play["serial"] == 1 for play in plays))
            self.assertTrue(all(play["any_errors_fatal"] is True for play in plays))

        provisioning = provision[1]
        maintenance = maintain[0]
        self.assertIs(provisioning["vars"]["os_reboot_enabled"], False)
        self.assertIs(maintenance["vars"]["os_reboot_enabled"], False)

        provisioning_pre = provisioning["pre_tasks"]
        provisioning_post = provisioning["post_tasks"]
        pre_update = unique_task_index(
            provisioning_pre,
            "ansible.builtin.import_role",
            {"name": "security_baseline", "tasks_from": "pre-update.yml"},
        )
        full_update = unique_task_index(
            provisioning_pre,
            "ansible.builtin.import_role",
            {"name": "system_maintenance", "tasks_from": "full-update.yml"},
        )
        security_post_update = unique_task_index(
            provisioning_pre,
            "ansible.builtin.import_role",
            {"name": "security_baseline", "tasks_from": "post-update.yml"},
        )
        automatic_updates = unique_task_index(
            provisioning_pre,
            "ansible.builtin.import_role",
            {"name": "system_maintenance", "tasks_from": "automatic-updates.yml"},
        )
        reboot_state = unique_task_index(
            provisioning_pre,
            "ansible.builtin.import_role",
            {"name": "system_maintenance", "tasks_from": "reboot-state.yml"},
        )
        combine = unique_fact_task_index(provisioning_pre, "os_reboot_required")
        assert_combined_reboot_fact(provisioning_pre[combine])
        reboot = unique_task_index(provisioning_pre, "ansible.builtin.reboot", None)
        self.assertEqual(
            ["os_reboot_required | bool", "os_reboot_enabled | bool"],
            provisioning_pre[reboot]["when"],
        )
        record = unique_fact_task_index(provisioning_pre, "os_reboot_performed")
        self.assertEqual(reboot + 1, record)
        assert_reboot_result_is_recorded(
            provisioning_pre[reboot], provisioning_pre[record]
        )
        reset = unique_task_index(
            provisioning_pre, "ansible.builtin.meta", "reset_connection"
        )
        setup = unique_task_index(provisioning_pre, "ansible.builtin.setup", None)
        post_boot = unique_task_index(
            provisioning_pre,
            "ansible.builtin.import_role",
            {"name": "security_baseline", "tasks_from": "mac.yml"},
        )
        for index in (reset, setup, post_boot):
            self.assertEqual("os_reboot_performed | bool", provisioning_pre[index]["when"])
        assert_strictly_ordered(
            pre_update,
            full_update,
            security_post_update,
            automatic_updates,
            reboot_state,
            combine,
            reboot,
            record,
            reset,
            setup,
            post_boot,
        )
        self.assertEqual(1, len(provisioning_post))
        verifier = unique_task_index(
            provisioning_post,
            "ansible.builtin.import_role",
            {"name": "os_baseline_verify"},
        )
        self.assertEqual(0, verifier)

        maintenance_pre = maintenance["pre_tasks"]
        maintenance_post = maintenance["post_tasks"]
        assert_scheduler_neutral_maintenance(maintenance)
        maintenance_update = unique_task_index(
            maintenance_pre,
            "ansible.builtin.import_role",
            {"name": "system_maintenance", "tasks_from": "full-update.yml"},
        )
        maintenance_reboot_state = unique_task_index(
            maintenance_pre,
            "ansible.builtin.import_role",
            {"name": "system_maintenance", "tasks_from": "reboot-state.yml"},
        )
        maintenance_combine = unique_fact_task_index(
            maintenance_pre, "os_reboot_required"
        )
        assert_combined_reboot_fact(maintenance_pre[maintenance_combine])
        report = unique_module_index(maintenance_pre, "ansible.builtin.debug")
        self.assertEqual(
            "A reboot is required. Re-run with -e os_reboot_enabled=true to authorize the reboot.",
            normalize_expression(maintenance_pre[report]["ansible.builtin.debug"]["msg"]),
        )
        self.assertEqual(
            ["os_reboot_required | bool", "not (os_reboot_enabled | bool)"],
            maintenance_pre[report]["when"],
        )
        maintenance_reboot = unique_task_index(
            maintenance_pre, "ansible.builtin.reboot", None
        )
        self.assertEqual(
            ["os_reboot_required | bool", "os_reboot_enabled | bool"],
            maintenance_pre[maintenance_reboot]["when"],
        )
        maintenance_record = unique_fact_task_index(
            maintenance_pre, "os_reboot_performed"
        )
        self.assertEqual(maintenance_reboot + 1, maintenance_record)
        assert_reboot_result_is_recorded(
            maintenance_pre[maintenance_reboot], maintenance_pre[maintenance_record]
        )
        maintenance_reset = unique_task_index(
            maintenance_pre, "ansible.builtin.meta", "reset_connection"
        )
        maintenance_setup = unique_task_index(
            maintenance_pre, "ansible.builtin.setup", None
        )
        for index in (maintenance_reset, maintenance_setup):
            self.assertEqual(
                "os_reboot_performed | bool", maintenance_pre[index]["when"]
            )
        assert_strictly_ordered(
            maintenance_update,
            maintenance_reboot_state,
            maintenance_combine,
            report,
            maintenance_reboot,
            maintenance_record,
            maintenance_reset,
            maintenance_setup,
        )
        self.assertEqual(1, len(maintenance_post))
        maintenance_verifier = unique_task_index(
            maintenance_post,
            "ansible.builtin.import_role",
            {"name": "os_baseline_verify"},
        )
        self.assertEqual(0, maintenance_verifier)
        self.assertEqual(
            "ansible_os_family != 'Archlinux'",
            maintenance_post[maintenance_verifier]["when"],
        )

    def test_os_playbook_contracts_reject_unsafe_composition_mutations(self) -> None:
        provision, maintenance = (
            load_yaml_documents(path)[0]
            for path in ("playbooks/os/provision.yml", "playbooks/os/maintain.yml")
        )
        combined_index = unique_fact_task_index(
            provision[1]["pre_tasks"], "os_reboot_required"
        )
        bad_combination = deepcopy(provision[1]["pre_tasks"][combined_index])
        bad_combination["ansible.builtin.set_fact"]["os_reboot_required"] = (
            COMBINED_REBOOT_EXPRESSION.replace("\nor ", "\nand ")
        )
        with self.assertRaises(AssertionError):
            assert_combined_reboot_fact(bad_combination)

        for play in (provision[1], maintenance[0]):
            pre_tasks = play["pre_tasks"]
            reboot = unique_task_index(pre_tasks, "ansible.builtin.reboot", None)
            record = unique_fact_task_index(pre_tasks, "os_reboot_performed")
            missing_register = deepcopy(pre_tasks[reboot])
            del missing_register["register"]
            with self.assertRaises(AssertionError):
                assert_reboot_result_is_recorded(missing_register, pre_tasks[record])

            mismatched_register = deepcopy(pre_tasks[reboot])
            mismatched_register["register"] = "different_reboot_result"
            with self.assertRaises(AssertionError):
                assert_reboot_result_is_recorded(mismatched_register, pre_tasks[record])

        reordered = deepcopy(provision[1]["pre_tasks"])
        reboot_state = unique_task_index(
            reordered,
            "ansible.builtin.import_role",
            {"name": "system_maintenance", "tasks_from": "reboot-state.yml"},
        )
        combine = unique_fact_task_index(reordered, "os_reboot_required")
        moved = reordered.pop(reboot_state)
        reordered.insert(combine + 1, moved)
        with self.assertRaises(AssertionError):
            assert_strictly_ordered(
                unique_task_index(
                    reordered,
                    "ansible.builtin.import_role",
                    {"name": "security_baseline", "tasks_from": "post-update.yml"},
                ),
                unique_task_index(
                    reordered,
                    "ansible.builtin.import_role",
                    {"name": "system_maintenance", "tasks_from": "reboot-state.yml"},
                ),
                unique_fact_task_index(reordered, "os_reboot_required"),
            )

        for section in ("roles", "tasks"):
            scheduled = deepcopy(maintenance[0])
            scheduled[section] = []
            with self.subTest(section=section):
                with self.assertRaises(AssertionError):
                    assert_scheduler_neutral_maintenance(scheduled)

        scheduled_task = deepcopy(maintenance[0])
        scheduled_task["pre_tasks"].append(
            {"ansible.builtin.cron": {"name": "maintenance", "minute": "0"}}
        )
        with self.assertRaises(AssertionError):
            assert_scheduler_neutral_maintenance(scheduled_task)

    def test_os_maintenance_rejects_unsupported_distribution_releases(self) -> None:
        """Full maintenance must stop before updating unsupported family members."""
        unsupported = (
            {"ansible_os_family": "Debian", "ansible_distribution": "Debian", "ansible_distribution_major_version": "12"},
            {"ansible_os_family": "RedHat", "ansible_distribution": "RedHat", "ansible_distribution_major_version": "9"},
        )
        for facts in unsupported:
            with self.subTest(facts=facts):
                result = subprocess.run(
                    [
                        "ansible-playbook",
                        "playbooks/os/maintain.yml",
                        "--inventory",
                        "servers,",
                        "--connection",
                        "local",
                        "--limit",
                        "servers",
                        "--check",
                        "--extra-vars",
                        str({"ansible_become": False, **facts}),
                    ],
                    cwd=REPOSITORY_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "maintenance supports Debian 13, Rocky Linux 9, and Arch Linux only",
                    output,
                )

    def test_complete_provisioning_starts_without_facts_or_become(self) -> None:
        first = load_yaml_documents("playbooks/os/provision.yml")[0][0]
        self.assertIs(first["gather_facts"], False)
        self.assertIs(first["become"], False)
        self.assertEqual(["os_bootstrap"], first["roles"])

    def test_os_baseline_verifier_is_observational(self) -> None:
        task_paths = sorted(
            (REPOSITORY_ROOT / "roles/os_baseline_verify/tasks").glob("*.yml")
        )
        self.assertTrue(task_paths)
        for path in task_paths:
            for task in load_tasks(str(path.relative_to(REPOSITORY_ROOT))):
                assert_observational_verifier_task(task)

        with self.assertRaises(AssertionError):
            assert_observational_verifier_task({"ansible.builtin.file": {}})
        with self.assertRaises(AssertionError):
            assert_observational_verifier_task(
                {"ansible.builtin.command": {"argv": ["/usr/bin/firewall-cmd", "--reload"]}, "changed_when": False}
            )
        with self.assertRaises(AssertionError):
            assert_observational_verifier_task({"command": "/usr/bin/rm -f /tmp/x", "changed_when": False})
        with self.assertRaises(AssertionError):
            assert_observational_verifier_task({"command": {"argv": "prefix /usr/bin/dpkg --audit"}, "changed_when": False})
        with self.assertRaises(AssertionError):
            assert_observational_verifier_task(
                {
                    "command": {"argv": "{{ ['/usr/bin/firewall-cmd', item] }}"},
                    "loop": ["--reload"],
                    "changed_when": False,
                }
            )
        for argv in (
            ["/usr/bin/systemctl", "restart", "chronyd.service"],
            ["/usr/bin/systemctl", "enable", "systemd-timesyncd.service"],
            ["/usr/bin/timedatectl", "set-ntp", "true"],
            ["/usr/bin/chronyc", "makestep"],
            ["/usr/bin/chronyc", "burst", "4/4"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(AssertionError):
                    assert_observational_verifier_task(
                        {"command": {"argv": argv}, "changed_when": False}
                    )
        for wrapper in ("block", "rescue", "always"):
            with self.subTest(wrapper=wrapper):
                assert_observational_verifier_task({wrapper: [{"assert": {"that": ["true"]}}]})
                with self.assertRaises(AssertionError):
                    assert_observational_verifier_task({wrapper: [{"file": {"path": "/tmp/x"}}]})

    def test_os_baseline_verifier_script_contract_rejects_missing_and_mutating_sources(self) -> None:
        with self.assertRaises(AssertionError):
            assert_observational_script(REPOSITORY_ROOT / "roles/os_baseline_verify/files/missing.py")
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "discover_management_interface.py"
            for contents in (
                "import subprocess\nfrom subprocess import run\n"
                "subprocess.run(['/usr/sbin/ip', '-json', 'route', 'get', 'peer'])\n"
                "run(['/usr/bin/touch', '/tmp/x'])\n",
                "import subprocess as process\n"
                "process.run(['/usr/sbin/ip', '-json', 'route', 'get', 'peer'])\n",
            ):
                source.write_text(contents, encoding="utf-8")
                with self.assertRaises(AssertionError):
                    assert_observational_script(source)

    def test_os_bootstrap_uses_only_raw_until_python_is_available(self) -> None:
        tasks = load_tasks("roles/os_bootstrap/tasks/main.yml")
        python_task = next(
            task for task in tasks
            if task["name"] == "Install Python only when it is absent"
        )
        python_index = tasks.index(python_task)

        self.assertTrue(
            all(
                "ansible.builtin.raw" in task
                for task in tasks[: python_index + 1]
            )
        )
        self.assertIn("sudo -n", python_task["ansible.builtin.raw"])
        self.assertNotIn("become", python_task)
        self.assertEqual(
            "'HOMELAB_PYTHON_INSTALLED' in os_bootstrap_python.stdout",
            python_task["changed_when"],
        )

        post_bootstrap_task = next(
            task for task in tasks
            if task["name"] == "Validate facts and passwordless sudo after bootstrap"
        )
        sudo_task = next(
            task for task in post_bootstrap_task["block"]
            if task["name"] == "Recheck non-interactive sudo"
        )
        self.assertEqual(
            ["sudo", "-n", "true"],
            sudo_task["ansible.builtin.command"]["argv"],
        )

    def test_os_bootstrap_has_no_root_or_password_fallback(self) -> None:
        source = (
            REPOSITORY_ROOT / "roles/os_bootstrap/tasks/main.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("id -un", source)
        self.assertIn("sudo -n true", source)
        self.assertNotIn("ansible_password", source)
        self.assertNotIn("ansible_become_password", source)
        self.assertNotIn("PermitRootLogin", source)

    def test_os_provision_rejects_arch_before_configuration_roles(self) -> None:
        """Complete provisioning must not apply unsupported hardening to Arch."""
        result = subprocess.run(
            [
                "ansible-playbook",
                "playbooks/os/provision.yml",
                "--inventory",
                "servers,",
                "--connection",
                "local",
                "--limit",
                "servers",
                "--start-at-task",
                "Validate complete provisioning platform support",
                "--extra-vars",
                '{"ansible_become": false, "ansible_os_family": "Archlinux"}',
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not support Archlinux", output)
        self.assertNotIn("Install common packages on Arch Linux", output)

    def test_frozen_k3s_manifest_templates_resolve_to_files(self) -> None:
        """Every configured local K3s manifest template must exist."""
        variables = load_yaml_documents(
            "inventory/frozen/k3s/group_vars/k3s_cluster/vars.yml"
        )[0]
        playbook_dir = REPOSITORY_ROOT / "playbooks/k3s"
        template_prefix = "{{ playbook_dir }}/"
        configured_templates = variables.get("k3s_server_manifests_templates")

        self.assertIsInstance(configured_templates, list)
        self.assertTrue(configured_templates)
        for configured_template in configured_templates:
            self.assertTrue(configured_template.startswith(template_prefix))
            relative_template = configured_template.removeprefix(template_prefix)
            resolved_template = (playbook_dir / relative_template).resolve()
            self.assertTrue(resolved_template.is_relative_to(REPOSITORY_ROOT))
            self.assertTrue(
                resolved_template.is_file(),
                f"configured K3s manifest template does not exist: {relative_template}",
            )

    def test_cifs_module_load_uses_available_builtin_with_truthful_status(self) -> None:
        """The best-effort module load must use a resolved action without changes."""
        tasks = load_tasks("roles/prepare_cifs_storage/tasks/setup-Debian.yml")
        module_load_tasks = [
            task
            for task in tasks
            if task["name"] == "Load required kernel module dm_crypt"
        ]

        self.assertEqual(len(module_load_tasks), 1)
        module_load = module_load_tasks[0]
        self.assertEqual(module_load.get("ansible.builtin.command"), "modprobe dm_crypt")
        self.assertIs(module_load.get("changed_when"), False)
        self.assertIs(module_load.get("failed_when"), False)
        self.assertNotIn("ignore_errors", module_load)

    def test_enable_cgroup_preserves_existing_cmdline_mode(self) -> None:
        """Cgroup installation and removal must retain the cmdline file mode."""
        for action in ("install", "uninstall"):
            tasks = load_tasks(f"roles/enable_cgroup/tasks/{action}.yml")
            stat_task = next(task for task in tasks if "ansible.builtin.stat" in task)
            copy_task = next(task for task in tasks if "ansible.builtin.copy" in task)

            self.assertEqual(
                stat_task["register"],
                "enable_cgroup_cmdline_stat",
            )
            self.assertEqual(
                copy_task["ansible.builtin.copy"]["mode"],
                "{{ enable_cgroup_cmdline_stat.stat.mode }}",
            )

    def test_system_maintenance_dispatches_supported_operating_systems(self) -> None:
        """Debian and RedHat hosts must resolve to maintained task files."""
        tasks = load_tasks("roles/system_maintenance/tasks/main.yml")
        first_task = tasks[0]

        self.assertIn("ansible.builtin.assert", first_task)
        self.assertEqual(
            first_task["ansible.builtin.assert"],
            {
                "that": [
                    "ansible_os_family in ['Debian', 'RedHat']"
                ],
                "fail_msg": (
                    "system-maintenance does not support operating-system family "
                    "received {{ ansible_os_family }}"
                ),
            },
        )
        self.assertEqual(
            [task["ansible.builtin.import_tasks"] for task in tasks[1:]],
            ["full-update.yml", "automatic-updates.yml", "reboot-state.yml"],
        )
        for os_family in ("Debian", "RedHat"):
            for task_file in (
                f"setup-{os_family}.yml",
                f"automatic-updates-{os_family}.yml",
                f"reboot-state-{os_family}.yml",
            ):
                self.assertTrue(
                    (
                        REPOSITORY_ROOT
                        / "roles/system_maintenance/tasks"
                        / task_file
                    ).is_file(),
                    f"missing system-maintenance tasks: {task_file}",
                )

    def test_prepare_cifs_storage_dispatches_only_implemented_families(self) -> None:
        """Every accepted CIFS operating-system family must have setup tasks."""
        tasks = load_tasks("roles/prepare_cifs_storage/tasks/main.yml")
        dispatch_task = tasks[0]
        supported_families = ("Debian",)

        self.assertEqual(
            "setup-{{ ansible_os_family }}.yml",
            dispatch_task["ansible.builtin.include_tasks"],
        )
        self.assertEqual(
            "ansible_os_family in ['Debian']",
            dispatch_task["when"],
        )
        for os_family in supported_families:
            self.assertTrue(
                (
                    REPOSITORY_ROOT
                    / "roles/prepare_cifs_storage/tasks"
                    / f"setup-{os_family}.yml"
                ).is_file(),
                f"missing prepare-cifs-storage tasks for {os_family}",
            )

    def test_system_maintenance_never_reboots_or_reduces_kernel_retention(
        self,
    ) -> None:
        task_paths = [
            "roles/system_maintenance/tasks/setup-Debian.yml",
            "roles/system_maintenance/tasks/setup-RedHat.yml",
        ]
        tasks = [task for path in task_paths for task in load_tasks(path)]

        self.assertFalse(any("ansible.builtin.reboot" in task for task in tasks))
        self.assertFalse(
            any(
                task.get("community.general.ini_file", {}).get("option")
                == "installonly_limit"
                for task in tasks
            )
        )

    def test_security_baseline_access_inputs_fail_closed(self) -> None:
        defaults = load_yaml_documents(
            "roles/security_baseline/defaults/main.yml"
        )[0]
        self.assertEqual([], defaults["security_baseline_authorized_keys"])
        self.assertEqual([], defaults["security_baseline_management_sources"])
        self.assertEqual([], defaults["security_baseline_firewall_services"])

    def test_security_baseline_validates_sudo_and_owns_keys_authoritatively(
        self,
    ) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/access.yml")
        sudo = next(
            task
            for task in tasks
            if task["name"] == "Install validated ansible sudo policy"
        )
        keys = next(
            task
            for task in tasks
            if task["name"] == "Reconcile authorized controller keys"
        )

        self.assertEqual(
            "/usr/sbin/visudo -cf %s",
            sudo["ansible.builtin.template"]["validate"],
        )
        self.assertEqual("0440", sudo["ansible.builtin.template"]["mode"])
        self.assertIs(keys["ansible.posix.authorized_key"]["exclusive"], True)
        self.assertIs(keys["no_log"], True)

    def test_firewall_policy_is_private_source_only_and_runtime_guarded(
        self,
    ) -> None:
        source = (
            REPOSITORY_ROOT / "roles/security_baseline/tasks/firewall.yml"
        ).read_text(encoding="utf-8")
        rule_source = (
            REPOSITORY_ROOT
            / "roles/security_baseline/filter_plugins/platform_controls.py"
        ).read_text(encoding="utf-8")
        self.assertIn("security_baseline_management_sources", source)
        self.assertIn('service name=\"ssh\"', rule_source)
        self.assertIn("security_baseline_apply_firewall_runtime", source)
        for forbidden in ("tailscale0", "http", "https", "dns", "public"):
            self.assertNotIn(forbidden, (source + rule_source).lower())

    def test_platform_mac_policy_does_not_mix_frameworks(self) -> None:
        tasks = load_tasks("roles/security_baseline/tasks/mac.yml")
        selinux = next(
            task
            for task in tasks
            if task.get("ansible.posix.selinux", {}).get("state") == "enforcing"
        )
        apparmor = next(
            task
            for task in tasks
            if task["name"] == "Install Debian AppArmor packages"
        )
        self.assertEqual("targeted", selinux["ansible.posix.selinux"]["policy"])
        self.assertEqual("enforcing", selinux["ansible.posix.selinux"]["state"])
        self.assertIn("ansible_os_family == 'RedHat'", selinux["when"])
        self.assertIn("ansible_os_family == 'Debian'", apparmor["when"])

    def test_logging_uses_persistent_bounded_journal_and_vendor_audit_rules(
        self,
    ) -> None:
        template = (
            REPOSITORY_ROOT
            / "roles/security_baseline/templates/journald-homelab.conf.j2"
        ).read_text(encoding="utf-8")
        audit_source = (
            REPOSITORY_ROOT / "roles/security_baseline/tasks/logging.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Storage=persistent", template)
        self.assertIn("SystemMaxUse=", template)
        self.assertIn("SystemKeepFree=", template)
        self.assertNotIn("audit.rules", audit_source)

    def test_time_provider_uses_platform_defaults(self) -> None:
        """A provider change must not edit operator-owned time sources."""
        defaults = load_yaml_documents("roles/security_baseline/defaults/main.yml")[0]
        tasks = load_tasks("roles/security_baseline/tasks/pre-update.yml")
        tasks_by_name = {task["name"]: task for task in tasks}
        self.assertIn("Install Debian repository trust and time packages", tasks_by_name)
        self.assertIn("Install Rocky repository trust and time packages", tasks_by_name)
        self.assertIn("Enable and start platform time synchronization", tasks_by_name)
        debian = tasks_by_name["Install Debian repository trust and time packages"]
        rocky = tasks_by_name["Install Rocky repository trust and time packages"]
        runtime = tasks_by_name["Enable and start platform time synchronization"]

        self.assertNotIn("security_baseline_chrony_sources", defaults)
        self.assertEqual(
            {"name": ["ca-certificates", "systemd-timesyncd"], "state": "present", "fail_on_autoremove": True, "lock_timeout": 300},
            debian["ansible.builtin.apt"],
        )
        self.assertEqual("ansible_os_family == 'Debian'", debian["when"])
        self.assertNotIn("security_baseline_apply_time_runtime", str(debian))
        self.assertEqual(
            {"name": ["ca-certificates", "chrony"], "state": "present", "lock_timeout": 300},
            rocky["ansible.builtin.dnf"],
        )
        self.assertEqual("ansible_os_family == 'RedHat'", rocky["when"])
        self.assertNotIn("security_baseline_apply_time_runtime", str(rocky))
        self.assertEqual(
            "{{ 'systemd-timesyncd' if ansible_os_family == 'Debian' else 'chronyd' }}",
            runtime["ansible.builtin.systemd_service"]["name"],
        )
        self.assertEqual("security_baseline_apply_time_runtime | bool", runtime["when"])
        source = (REPOSITORY_ROOT / "roles/security_baseline/tasks/pre-update.yml").read_text(encoding="utf-8")
        self.assertNotIn("blockinfile", source)
        self.assertNotIn("replace", source)
        self.assertNotIn("chrony.conf", source)
        self.assertNotIn("Restart chrony", (REPOSITORY_ROOT / "roles/security_baseline/handlers/main.yml").read_text(encoding="utf-8"))
        self.assertFalse((REPOSITORY_ROOT / "roles/os_baseline_verify/files/discover_chrony_sources.py").exists())

    def test_time_verifier_is_provider_native_and_observational(self) -> None:
        tasks = load_tasks("roles/os_baseline_verify/tasks/platform.yml")
        tasks_by_name = {task["name"]: task for task in tasks}
        self.assertIn("Verify native MAC and time package evidence", tasks_by_name)
        package_evidence = tasks_by_name["Verify native MAC and time package evidence"]
        expected_commands = {
            "Read Debian time service enablement": ["/usr/bin/systemctl", "is-enabled", "systemd-timesyncd.service"],
            "Read Debian time service activity": ["/usr/bin/systemctl", "is-active", "systemd-timesyncd.service"],
            "Read Debian time synchronization state": ["/usr/bin/timedatectl", "show", "--property=NTPSynchronized", "--value"],
            "Read Rocky time service enablement": ["/usr/bin/systemctl", "is-enabled", "chronyd.service"],
            "Read Rocky time service activity": ["/usr/bin/systemctl", "is-active", "chronyd.service"],
            "Read Rocky chrony tracking state": ["/usr/bin/chronyc", "tracking"],
            "Read Rocky chrony source state": ["/usr/bin/chronyc", "sources"],
        }

        self.assertNotIn("when", package_evidence)
        self.assertIn("systemd-timesyncd", str(package_evidence))
        self.assertIn("chrony", str(package_evidence))
        self.assertNotIn("discover_chrony_sources", str(tasks))
        self.assertNotIn("os_baseline_verify_chrony_effective_policy", str(tasks))
        for name, argv in expected_commands.items():
            with self.subTest(name=name):
                self.assertIn(name, tasks_by_name)
                task = tasks_by_name[name]
                self.assertEqual(argv, task["ansible.builtin.command"]["argv"])
                self.assertIs(False, task["changed_when"])
                self.assertEqual(
                    ["os_baseline_verify_runtime_controls | bool", "ansible_os_family == 'Debian'" if "Debian" in name else "ansible_os_family == 'RedHat'"],
                    task["when"],
                )

    def test_verifier_debian_updater_asserts_the_effective_origin_list(self) -> None:
        tasks = load_tasks("roles/os_baseline_verify/tasks/updates.yml")
        verify = next(task for task in tasks if task["name"] == "Verify Debian security-only updater configuration")
        self.assertTrue(
            any("os_baseline_verify_debian_policy.origins" in assertion for assertion in verify["ansible.builtin.assert"]["that"])
        )

    def test_verifier_firewall_reducers_remain_bound_to_policy_assertions(self) -> None:
        tasks = load_tasks("roles/os_baseline_verify/tasks/firewall.yml")
        permanent_reducer = next(task for task in tasks if task["name"] == "Reduce permanent firewall state from ordered reads")
        runtime_reducer = next(task for task in tasks if task["name"] == "Reduce runtime firewall state from ordered reads")
        permanent_assert = next(task for task in tasks if task["name"] == "Verify permanent firewall default and exact policy")
        runtime_assert = next(task for task in tasks if task["name"] == "Verify exact runtime firewall policy")

        self.assertIn("os_baseline_verify_firewall_state_from_results", str(permanent_reducer))
        self.assertIn("os_baseline_verify_permanent_firewall_state", str(permanent_assert))
        self.assertIn("os_baseline_verify_firewall_state_from_results", str(runtime_reducer))
        self.assertIn("os_baseline_verify_runtime_firewall_state", str(runtime_assert))

    def test_system_maintenance_managed_packages_are_minimal(self) -> None:
        source = "\n".join(
            (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
            for path in (
                "roles/system_maintenance/tasks/setup-Debian.yml",
                "roles/system_maintenance/tasks/setup-RedHat.yml",
            )
        )
        for forbidden in (
            "qemu-guest-agent",
            "xterm",
            "vim",
            "tree",
            "git",
            "python3-pip",
            "python3-venv",
            "python3-netaddr",
            "apache2-utils",
            "httpd-tools",
            "epel-release",
            "fail2ban",
        ):
            with self.subTest(package=forbidden):
                self.assertNotIn(forbidden, source)

    def test_native_security_updates_are_daily_and_reboot_controllable(self) -> None:
        defaults = load_yaml_documents(
            "roles/system_maintenance/defaults/main.yml"
        )[0]
        self.assertEqual(
            "*-*-* 04:00:00",
            defaults["system_maintenance_security_update_calendar"],
        )
        self.assertEqual(
            "04:30",
            defaults["system_maintenance_security_reboot_time"],
        )
        self.assertIs(
            defaults["system_maintenance_native_reboot_enabled"],
            True,
        )

    def test_system_maintenance_configures_native_security_updater_mechanisms(
        self,
    ) -> None:
        platform_contracts = {
            "Debian": {
                "module": "ansible.builtin.apt",
                "package": "unattended-upgrades",
                "timer": "apt-daily-upgrade.timer",
            },
            "RedHat": {
                "module": "ansible.builtin.dnf",
                "package": "dnf-automatic",
                "timer": "dnf-automatic.timer",
            },
        }
        for os_family, contract in platform_contracts.items():
            with self.subTest(os_family=os_family):
                tasks = load_tasks(
                    "roles/system_maintenance/tasks/"
                    f"automatic-updates-{os_family}.yml"
                )
                package_tasks = [
                    task
                    for task in tasks
                    if task.get(contract["module"], {}).get("name")
                    == contract["package"]
                ]
                self.assertEqual(1, len(package_tasks))
                if not package_tasks:
                    continue

                package_task = package_tasks[0]
                self.assertEqual(
                    {
                        "name": contract["package"],
                        "state": "present",
                        "lock_timeout": (
                            "{{ system_maintenance_package_lock_timeout }}"
                        ),
                    },
                    package_task[contract["module"]],
                )
                timer_task = next(
                    task for task in tasks
                    if "ansible.builtin.systemd_service" in task
                )
                self.assertEqual(
                    {
                        "name": contract["timer"],
                        "enabled": True,
                        "state": "started",
                        "daemon_reload": True,
                    },
                    timer_task["ansible.builtin.systemd_service"],
                )
                self.assertLess(tasks.index(package_task), tasks.index(timer_task))

        apt_policy = (
            REPOSITORY_ROOT
            / "roles/system_maintenance/templates/apt-unattended-upgrades.j2"
        ).read_text(encoding="utf-8")
        apt_origins = [
            line.strip()
            for line in apt_policy.splitlines()
            if line.strip().startswith('"origin=')
        ]
        self.assertEqual(
            [
                '"origin=Debian,codename=${distro_codename}-security,'
                'label=Debian-Security";'
            ],
            apt_origins,
        )

        dnf_policy = (
            REPOSITORY_ROOT
            / "roles/system_maintenance/templates/dnf-automatic.conf.j2"
        ).read_text(encoding="utf-8")
        dnf_policy = dnf_policy.replace(
            "{{ 'when-needed' if system_maintenance_native_reboot_enabled "
            "else 'never' }}",
            "when-needed",
        )
        dnf_config = configparser.ConfigParser()
        dnf_config.read_string(dnf_policy)
        self.assertEqual("security", dnf_config["commands"]["upgrade_type"])
        self.assertEqual("yes", dnf_config["commands"]["download_updates"])
        self.assertEqual("yes", dnf_config["commands"]["apply_updates"])

        self.assertEqual(
            [],
            load_tasks(
                "roles/system_maintenance/tasks/automatic-updates-Archlinux.yml"
            ),
        )

    def test_system_maintenance_idempotence_skips_only_live_upgrades(self) -> None:
        """Live upgrades must run during converge but not strict idempotence."""
        imported_task_files = [
            "setup-Debian.yml",
            "setup-RedHat.yml",
        ]
        playbook = [
            {
                "name": "List system maintenance tasks",
                "hosts": "localhost",
                "gather_facts": False,
                "tasks": [
                    {
                        "name": f"Import {task_file}",
                        "ansible.builtin.import_tasks": str(
                            REPOSITORY_ROOT
                            / "roles/system_maintenance/tasks"
                            / task_file
                        ),
                    }
                    for task_file in imported_task_files
                ],
            }
        ]

        with tempfile.TemporaryDirectory() as temporary_directory:
            playbook_path = Path(temporary_directory) / "list-maintenance.yml"
            playbook_path.write_text(
                yaml.safe_dump(playbook, sort_keys=False),
                encoding="utf-8",
            )

            def list_tasks(*extra_arguments: str) -> str:
                result = subprocess.run(
                    [
                        "ansible-playbook",
                        str(playbook_path),
                        "--inventory",
                        "localhost,",
                        "--connection",
                        "local",
                        "--list-tasks",
                        *extra_arguments,
                    ],
                    cwd=REPOSITORY_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                return result.stdout

            converge_tasks = list_tasks()
            idempotence_tasks = list_tasks(
                "--skip-tags", "molecule-idempotence-notest"
            )

        live_upgrade_tasks = [
            "Update package cache and upgrade all packages",
            "Fully update installed packages",
        ]
        for task_name in live_upgrade_tasks:
            with self.subTest(task_name=task_name):
                self.assertIn(task_name, converge_tasks)
                self.assertNotIn(task_name, idempotence_tasks)

        stable_tasks = [
            "Autoremove unused packages",
            "Install package-management utilities",
        ]
        for task_name in stable_tasks:
            with self.subTest(task_name=task_name):
                self.assertIn(task_name, idempotence_tasks)

    def test_update_pihole_has_no_end_play(self) -> None:
        """A Pi-hole command failure must fail normally instead of ending a play."""
        tasks = load_tasks("roles/update_pihole/tasks/main.yml")
        command_tasks = [
            task for task in tasks if "ansible.builtin.command" in task
        ]

        self.assertEqual(
            [
                (task["ansible.builtin.command"], task.get("changed_when"))
                for task in command_tasks
            ],
            [("pihole status", False)],
        )
        self.assertFalse(
            any("ignore_errors" in task for task in tasks),
        )
        self.assertFalse(
            any("failed_when" in task for task in tasks),
        )
        self.assertFalse(
            any(task.get("ansible.builtin.meta") == "end_play" for task in tasks)
        )

    def test_dns_commands_do_not_treat_nonzero_rc_as_changed(self) -> None:
        """DNS command failures must remain failures, never reported as changes."""
        tasks = load_tasks("roles/update_pihole/tasks/update-dns.yml")

        self.assertEqual(len(tasks), 2)
        self.assertTrue(
            all("ansible.builtin.command" in task for task in tasks)
        )
        self.assertTrue(
            all(task.get("changed_when") is True for task in tasks)
        )
        self.assertTrue(
            all("failed_when" not in task and "ignore_errors" not in task for task in tasks)
        )

    def test_verify_playbook_commands_are_read_only(self) -> None:
        """The operator verification playbook may only inspect Pi-hole and Unbound."""
        playbook_path = REPOSITORY_ROOT / "playbooks/pihole/verify.yml"
        self.assertTrue(playbook_path.is_file())
        play = load_yaml_documents("playbooks/pihole/verify.yml")[0][0]
        tasks = play["tasks"]

        self.assertEqual(play["hosts"], "pihole")
        self.assertTrue(play["become"])
        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            [
                (task.get("ansible.builtin.command"), task.get("changed_when"))
                for task in tasks
            ],
            [
                ("pihole status", False),
                ("unbound-checkconf /etc/unbound/unbound.conf", False),
            ],
        )

    def test_os_inspect_reports_only_allowlisted_facts(self) -> None:
        """Host inspection must remain read-only and omit identifying facts."""
        playbook_path = REPOSITORY_ROOT / "playbooks/os/inspect.yml"
        self.assertTrue(playbook_path.is_file())
        play = load_yaml_documents("playbooks/os/inspect.yml")[0][0]
        tasks = play["tasks"]

        allowed_facts = [
            "ansible_architecture",
            "ansible_distribution",
            "ansible_distribution_release",
            "ansible_distribution_version",
            "ansible_kernel",
            "ansible_os_family",
            "ansible_pkg_mgr",
            "ansible_python_version",
            "ansible_service_mgr",
            "ansible_virtualization_role",
            "ansible_virtualization_type",
        ]

        self.assertEqual(play["hosts"], "servers")
        self.assertIs(play["become"], False)
        self.assertIs(play["gather_facts"], False)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            tasks[0]["ansible.builtin.setup"],
            {
                "filter": allowed_facts,
                "gather_subset": [
                    "!all",
                    "!min",
                    "architecture",
                    "distribution",
                    "distribution_release",
                    "distribution_version",
                    "kernel",
                    "os_family",
                    "pkg_mgr",
                    "python_version",
                    "service_mgr",
                    "virtualization_role",
                    "virtualization_type",
                ],
            },
        )
        self.assertIs(tasks[0]["no_log"], True)
        reported_facts = {
            name: " ".join(expression.split())
            for name, expression in tasks[1]["ansible.builtin.debug"]["msg"].items()
        }
        self.assertEqual(
            reported_facts,
            {
                fact.removeprefix("ansible_"): (
                    "{{ inspected_host.ansible_facts."
                    f"{fact} | default('unknown', true) }}}}"
                )
                for fact in allowed_facts
            },
        )

    def test_ansible_cfg_uses_only_repository_role_paths(self) -> None:
        """Controller roles must resolve only from repository-managed locations."""
        config = configparser.ConfigParser()
        config.read(REPOSITORY_ROOT / "ansible.cfg")

        self.assertEqual(config["defaults"]["roles_path"], "./.ansible/roles:./roles")
        self.assertEqual(config["defaults"]["collections_path"], "./.ansible/collections")

    def test_ansible_cfg_does_not_disable_host_key_checking(self) -> None:
        """Host-key verification must remain enabled unless Ansible's default is used."""
        config = configparser.ConfigParser()
        config.read(REPOSITORY_ROOT / "ansible.cfg")

        if config.has_option("defaults", "host_key_checking"):
            self.assertEqual(config["defaults"]["host_key_checking"].strip().lower(), "true")


if __name__ == "__main__":
    unittest.main()
