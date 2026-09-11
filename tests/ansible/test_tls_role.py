"""Exercise actual role assertions through Ansible's registered loop results."""
import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles/tls_automation"
_spec = importlib.util.spec_from_file_location("tls_validation", ROLE / "filter_plugins/validation.py")
validation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validation)


def registered_units(absent=False, omit_arrays=False):
    records = []
    for unit, user, path, timeout in (
        ("issuer.service", "svc-acme", "/var/lib/homelab-tls-issuer", "15min"),
        ("renew.service", "root", "/var/lib/homelab-tls /etc/caddy /var/lib/homelab-reverse-proxy", "1h"),
        ("renew.timer", "", "", ""),
    ):
        name = "homelab-tls-" + unit
        properties = {"LoadState": "not-found" if absent else "loaded", "DropInPaths": "",
                      "FragmentPath": "" if absent else "/etc/systemd/system/" + name}
        if not absent and unit.endswith("service"):
            command = "/usr/local/libexec/homelab-tls-" + ("issue" if user == "svc-acme" else "reconcile")
            properties.update(User=user, Group=user, SupplementaryGroups="", Type="oneshot",
                              AmbientCapabilities="cap_setuid" if user == "root" else "",
                              NoNewPrivileges="yes", ProtectSystem="strict", PrivateTmp="yes",
                              ReadWritePaths=path, TimeoutStartUSec=timeout,
                              LoadCredential="cloudflare-token:/etc/homelab-tls/cloudflare-token" if user == "svc-acme" else "",
                              ExecCondition="", ExecStartPre="", ExecStartPost="",
                              ExecStart="{ path=" + command + " ; argv[]=" + command + " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0 }")
        elif not absent:
            properties.update(Unit="homelab-tls-renew.service", Persistent="yes", RandomizedDelayUSec="1h",
                              TimersCalendar="{ OnCalendar=*-*-* 00,12:00:00 ; next_elapse=n/a }")
        if omit_arrays:
            for key in ("ExecCondition", "ExecStartPre", "ExecStartPost", "LoadCredential"):
                properties.pop(key, None)
        records.append({"item": {"name": name}, "rc": 0,
                        "stdout": "\n".join(key + "=" + value for key, value in properties.items())})
    return {"changed": False, "results": records}


def registered_arrays(absent=False):
    records = []
    for name, credential in (("homelab-tls-issuer.service", 'a(ss) 1 "cloudflare-token" "/etc/homelab-tls/cloudflare-token"'),
                             ("homelab-tls-renew.service", 'a(ss) 0')):
        result = {"item": {"name": name}}
        if absent:
            result["skipped"] = True
        else:
            result.update(rc=0, stdout="a(sasbttttuii) 0\na(sasbttttuii) 0\na(sasbttttuii) 0\n" + credential + "\n")
        records.append(result)
    return {"changed": False, "results": records}


class RoleUnitTests(unittest.TestCase):
    def test_renewal_ambient_capability_is_exact_with_preflight_upgrade(self):
        for capability in ('', 'cap_setuid', 'cap_setuid cap_net_admin'):
            for preflight in (True, False):
                value = registered_units()
                value['results'][1]['stdout'] = value['results'][1]['stdout'].replace(
                    'AmbientCapabilities=cap_setuid', 'AmbientCapabilities=' + capability)
                with self.subTest(capability=capability, preflight=preflight):
                    if capability == 'cap_setuid' or (preflight and capability == ''):
                        self.assertTrue(validation.validate_units(value['results'], preflight, registered_arrays()['results']))
                    else:
                        with self.assertRaises(ValueError):
                            validation.validate_units(value['results'], preflight, registered_arrays()['results'])

    def test_previous_canonical_write_scope_can_upgrade_only_in_preflight(self):
        value = registered_units()
        value["results"][1]["stdout"] = value["results"][1]["stdout"].replace(
            "ReadWritePaths=/var/lib/homelab-tls /etc/caddy /var/lib/homelab-reverse-proxy",
            "ReadWritePaths=/var/lib/homelab-tls",
        )
        self.assertTrue(validation.validate_units(value["results"], True, registered_arrays()["results"]))
        with self.assertRaises(ValueError):
            validation.validate_units(value["results"], False, registered_arrays()["results"])
        value["results"][1]["stdout"] = value["results"][1]["stdout"].replace(
            "ReadWritePaths=/var/lib/homelab-tls", "ReadWritePaths=/etc"
        )
        with self.assertRaises(ValueError):
            validation.validate_units(value["results"], True, registered_arrays()["results"])

    def test_actual_role_assertions_accept_registered_results(self):
        tasks = []
        for filename in ("preflight.yml", "configure.yml", "timer.yml"):
            source = yaml.safe_load((ROLE / "tasks" / filename).read_text())
            assertion = next(task for task in source if "tls_automation_validate_units" in str(task))
            for omitted in (False, True):
                tasks.append(dict(assertion, vars={"tls_automation_unit_properties": registered_units(omit_arrays=omitted),
                                                   "tls_automation_service_arrays": registered_arrays()}))
            if filename == "preflight.yml":
                tasks.append(dict(assertion, vars={"tls_automation_unit_properties": registered_units(True),
                                                   "tls_automation_service_arrays": registered_arrays(True)}))
        with tempfile.TemporaryDirectory(prefix="tls-role-test-") as name:
            directory = Path(name)
            config = directory / "ansible.cfg"
            config.write_text("[defaults]\nfilter_plugins=" + str(ROLE / "filter_plugins") + "\n")
            playbook = directory / "test.yml"
            playbook.write_text(yaml.safe_dump([{"name": "Test role unit assertions", "hosts": "localhost",
                                               "gather_facts": False, "tasks": tasks}]))
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("ANSIBLE_", "SOPS_"))}
            environment.update(ANSIBLE_CONFIG=str(config), ANSIBLE_VARS_ENABLED="host_group_vars")
            result = subprocess.run([str(ROOT / ".venv/bin/ansible-playbook"), "-i", "localhost,",
                                     "-c", "local", str(playbook)], env=environment,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=45)
            self.assertEqual(0, result.returncode, result.stdout)

    def test_registered_service_commands_reject_ignored_failures_and_auxiliary_phases(self):
        for index in (0, 1):
            for original, replacement in (("ignore_errors=no", "ignore_errors=yes"),
                                           ("ExecCondition=\n", "ExecCondition=/usr/bin/true\n"),
                                           ("ExecStartPre=\n", "ExecStartPre=/usr/bin/true\n"),
                                           ("ExecStartPost=\n", "ExecStartPost=/usr/bin/true\n")):
                with self.subTest(index=index, replacement=replacement):
                    value = copy.deepcopy(registered_units())
                    value["results"][index]["stdout"] = value["results"][index]["stdout"].replace(original, replacement)
                    with self.assertRaises(ValueError):
                        validation.validate_units(value["results"], False, registered_arrays()["results"])

    def test_manual_renewal_uses_the_shared_blocking_start_task(self):
        source = yaml.safe_load((ROOT / "playbooks/tls/renew.yml").read_text())
        task = source[0]["tasks"][0]
        self.assertEqual("tasks/renew.yml", task.get("ansible.builtin.import_tasks"))
        shared = yaml.safe_load((ROOT / "playbooks/tls/tasks/renew.yml").read_text())
        self.assertEqual(["/usr/bin/systemctl", "start", "homelab-tls-renew.service"],
                         shared[0]["ansible.builtin.command"]["argv"])

    def test_complete_unit_listing_rejects_only_exact_tls_templates_and_instances(self):
        from jinja2 import Environment
        from ansible.plugins.test.core import TestModule
        tasks = yaml.safe_load((ROLE / "tasks/inspect-units.yml").read_text())
        assertion = tasks[-1]["ansible.builtin.assert"]["that"]
        environment = Environment()
        environment.tests.update(TestModule().tests())
        cases = [([], True), (["unrelated@fixture.service enabled"], True),
                 (["homelab-tls-renew.service enabled"], True),
                 (["homelab-tls-issuer@.service disabled"], False),
                 (["homelab-tls-issuer@fixture.service enabled"], False),
                 (["homelab-tls-renew@fixture.timer loaded active waiting"], False),
                 (["  homelab-tls-renew@fixture.service loaded active running"], False)]
        for records, accepted in cases:
            for variable in ("tls_automation_unit_file_instances", "tls_automation_loaded_unit_instances"):
                with self.subTest(records=records, variable=variable):
                    variables = {"tls_automation_unit_file_instances": {"stdout_lines": []},
                                 "tls_automation_loaded_unit_instances": {"stdout_lines": []}}
                    variables[variable] = {"stdout_lines": records}
                    self.assertEqual(accepted, all(environment.compile_expression(item)(**variables) for item in assertion))
        for task in tasks[2:4]:
            command = task["ansible.builtin.command"]["argv"]
            self.assertFalse(any("@" in part or "{{" in part for part in command))
            self.assertNotIn("failed_when", task)


    def test_typed_arrays_reject_missing_nonempty_or_wrong_credential_evidence(self):
        for index in (0, 1):
            for mutation in ("missing", "failed", "skipped", "nonempty0", "nonempty1", "nonempty2", "wrong_type", "wrong_credential", "duplicate"):
                with self.subTest(index=index, mutation=mutation):
                    arrays = registered_arrays()["results"]
                    target = arrays[index]
                    if mutation == "missing":
                        target["stdout"] = ""
                    elif mutation == "failed":
                        target["rc"] = 1
                    elif mutation == "skipped":
                        target["skipped"] = True
                    elif mutation.startswith("nonempty"):
                        lines = target["stdout"].splitlines()
                        lines[int(mutation[-1])] = 'a(sasbttttuii) 1 "/usr/bin/true" 1 "/usr/bin/true" false 0 0 0 0 0 0 0'
                        target["stdout"] = "\n".join(lines)
                    elif mutation == "wrong_type":
                        target["stdout"] = target["stdout"].replace("a(sasbttttuii)", "as", 1)
                    elif mutation == "wrong_credential":
                        target["stdout"] = "\n".join(target["stdout"].splitlines()[:3] + ['a(ss) 1 "other" "/other"'])
                    else:
                        arrays[1] = arrays[0]
                    with self.assertRaises(ValueError):
                        validation.validate_units(registered_units(omit_arrays=True)["results"], False, arrays)

    def test_baseline_tls_fixture_inputs_resolve_from_playbook_directory(self):
        from jinja2 import Environment, StrictUndefined
        baseline = ROOT / 'roles/system_maintenance/molecule/baseline'
        sources = [yaml.safe_load((baseline / 'verify.yml').read_text()),
                   yaml.safe_load((baseline / 'tasks/tls-manual-wait.yml').read_text())]
        expected = {
            'Install the metadata-only credential probe': ROOT / 'tests/tls/credential_probe.py',
            'Copy the secret-free TLS unit tests into disposable state': ROOT / 'tests/tls',
            'Install bounded synthetic oneshot program': ROOT / 'tests/tls/manual_wait_fixture.py',
            'Await manual reconciliation using the production task': ROOT / 'playbooks/tls/tasks/renew.yml',
        }
        found = set()
        environment = Environment(undefined=StrictUndefined)
        def inspect(value):
            if isinstance(value, list):
                for item in value:
                    inspect(item)
            elif isinstance(value, dict):
                name = value.get('name')
                if name in expected:
                    found.add(name)
                    source = value.get('ansible.builtin.include_tasks') or value['ansible.builtin.copy']['src']
                    rendered = environment.from_string(source).render(playbook_dir=str(baseline))
                    self.assertEqual(expected[name], Path(rendered).resolve())
                    self.assertTrue(expected[name].exists())
                for key in ('tasks', 'block', 'rescue', 'always'):
                    if key in value:
                        inspect(value[key])
        inspect(sources)
        self.assertEqual(set(expected), found)


    def test_synthetic_start_is_independent_and_reports_bounded_failure_metadata(self):
        baseline = ROOT / 'roles/system_maintenance/molecule/baseline'
        tasks = yaml.safe_load((baseline / 'tasks/tls-manual-wait.yml').read_text())
        fixture = next(task for task in tasks if 'block' in task)
        kickoff = next(task for task in fixture['block'] if task['name'].startswith('Begin the synthetic'))
        self.assertEqual(['/usr/bin/systemctl', 'start', '--no-block', '--job-mode=ignore-dependencies',
                          'homelab-tls-renew.service'], kickoff['ansible.builtin.command']['argv'])
        rescue = fixture.get('rescue', [])
        diagnostics = next(task for task in rescue if 'ansible.builtin.command' in task)
        self.assertEqual(['/usr/bin/systemctl', 'show', 'homelab-tls-renew.service', '--all',
                          '--property=Job,ActiveState,SubState,Result,ExecMainStatus,ExecMainCode,MainPID'],
                         diagnostics['ansible.builtin.command']['argv'])
        self.assertIn('ansible.builtin.fail', rescue[-1])
        self.assertTrue(fixture['always'][0]['name'].startswith('Stop the disposable'))

    def test_manual_fixture_preserves_sandbox_and_reports_process_identity(self):
        import json
        from unittest.mock import patch
        baseline = ROOT / 'roles/system_maintenance/molecule/baseline'
        fixture = next(task for task in yaml.safe_load((baseline / 'tasks/tls-manual-wait.yml').read_text())
                       if 'block' in task)
        override = next(task['ansible.builtin.copy']['content'] for task in fixture['block']
                        if task.get('ansible.builtin.copy', {}).get('dest', '').endswith('fixture.conf'))
        directives = [line for line in override.splitlines() if line and not line.startswith(('[', '#'))]
        self.assertEqual(['JobTimeoutSec', 'User', 'ExecStart', 'ExecStart', 'TimeoutStartSec'],
                         [line.split('=', 1)[0] for line in directives])
        self.assertIn('User=', directives)
        production = (ROLE / 'templates/homelab-tls-renew.service.j2').read_text().splitlines()
        for directive in ('User=root', 'Group=root', 'SupplementaryGroups=', 'NoNewPrivileges=yes',
                          'AmbientCapabilities=CAP_SETUID',
                          'PrivateDevices=yes', 'ProtectKernelTunables=yes', 'ProtectKernelModules=yes',
                          'LockPersonality=yes', 'RestrictSUIDSGID=yes',
                          'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6'):
            self.assertIn(directive, production)
        source = (ROOT / 'tests/tls/manual_wait_fixture.py').read_text()
        with tempfile.TemporaryDirectory() as directory:
            with patch('os.getuid', return_value=23), patch('os.geteuid', return_value=24), \
                 patch('os.getgid', return_value=25), patch('os.getegid', return_value=26), \
                 patch('os.getgroups', return_value=[27]), patch('time.sleep'), \
                 patch('sys.argv', ['wait.py', '7']), self.assertRaises(SystemExit) as result:
                exec(compile(source, 'wait.py', 'exec'), {'__file__': str(Path(directory) / 'wait.py')})
            self.assertEqual(7, result.exception.code)
            for name in ('started.json', 'completed.json'):
                evidence = json.loads((Path(directory) / name).read_text())
                self.assertEqual({'uid': 23, 'euid': 24, 'gid': 25, 'egid': 26, 'groups': [27]},
                                 {key: evidence[key] for key in ('uid', 'euid', 'gid', 'egid', 'groups')})
