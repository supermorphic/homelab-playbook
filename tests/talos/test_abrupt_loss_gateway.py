from __future__ import annotations

import hashlib
import json
import os
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class AbruptLossGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        self.bin.mkdir()
        self.state.mkdir()
        self.real_python = shutil.which("python3") or "/usr/bin/python3"
        self.real_bash = shutil.which("bash") or "/bin/bash"
        self._source()
        self._clients()
        self.request = self._request()

    def _write_executable(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content.replace("__FAKE_STATE__", str(self.state)), encoding="utf-8")
        path.chmod(0o755)

    def _source(self) -> None:
        source = self.root / "source"
        (source / "talos").mkdir(parents=True)
        (source / "kubernetes/apps/testing/echo/app").mkdir(parents=True)
        (source / "talos/talconfig.yaml").write_text(
            "endpoint: https://192.0.2.20:6443\nnodes:\n"
            "  - {hostname: node-a, ipAddress: 192.0.2.10, controlPlane: true}\n"
            "  - {hostname: node-b, ipAddress: 192.0.2.11, controlPlane: true}\n"
            "  - {hostname: node-c, ipAddress: 192.0.2.12, controlPlane: true}\n", encoding="utf-8")
        (source / "kubernetes/apps/testing/echo/app/httproute.yaml").write_text(
            "spec:\n  hostnames: [echo.example.test]\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(source), "config", key, value], check=True)
        subprocess.run(["git", "-C", str(source), "remote", "add", "origin", "https://github.com/supermorphic/homelab-talos.git"], check=True)
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture"], check=True)
        self.revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        cache = source / ".cache/recovery-helm"
        cache.mkdir(parents=True)
        charts = {}
        for logical in ("cilium", "cert-manager", "metallb", "envoy-gateway", "external-dns"):
            archive = cache / f"{logical}.tgz"
            archive.write_bytes(logical.encode())
            charts[logical] = {"file": archive.name, "chartName": "gateway-helm" if logical == "envoy-gateway" else logical,
                               "version": "1.0.0", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
        (cache / "manifest.json").write_text(json.dumps({"schemaVersion": 1, "sourceRevision": self.revision, "charts": charts}), encoding="utf-8")
        self.source = source

    def _clients(self) -> None:
        self._write_executable("bash", f"#!/bin/sh\ncase \"$1\" in *abrupt-loss-bridge.sh) exec {self.real_bash} -x \"$@\" 2>>__FAKE_STATE__/bridge.log;; *) exec {self.real_bash} \"$@\";; esac\n")
        self._write_executable("sleep", "#!/bin/sh\nexec /bin/sleep 0.05\n")
        self._write_executable("dig", "#!/bin/sh\necho 192.0.2.80\n")
        self._write_executable("curl", "#!/bin/sh\necho ok\n")
        self._write_executable("mise", """#!/usr/bin/env python3
import json,sys
p=json.load(open(sys.argv[-1])); c=p['credentials']; mode=p['mode']
print(json.dumps({'schemaVersion':1,'requestId':p['requestId'],'mode':mode,'node':p['node'],'sourceRevision':p['sourceRevision'],'kubeContext':c['kubeContext'],'talosContext':c['talosContext'],'checks':{'source':'passed','cilium':'not-run' if mode=='prepare' else 'passed','foundation':'not-run' if mode=='prepare' else 'passed'}}))
""")
        self._write_executable("talosctl", r"""#!/usr/bin/env python3
import os,sys
from pathlib import Path
a=sys.argv[1:]; off=(Path('__FAKE_STATE__')/'power-off').exists()
nodes=a[a.index('--nodes')+1] if '--nodes' in a else ''
target=nodes=='192.0.2.10'
if off and target and (('version' in a) or ('status' in a)):
 print('connection refused',file=sys.stderr); raise SystemExit(1)
if 'hostname' in a: print('spec:\n  hostname: node-a')
elif 'securitystate' in a: print('spec:\n  secureBoot: true\n  bootedWithUKI: true')
elif 'volumestatuses' in a:
 print('---\nmetadata: {id: STATE}\nspec: {phase: ready, encryptionProvider: luks2}\n---\nmetadata: {id: EPHEMERAL}\nspec: {phase: ready, encryptionProvider: luks2}\n---\nmetadata: {id: u-longhorn}\nspec: {phase: ready}')
elif 'members' in a: print('H\nx y node-a\nx y node-b\nx y node-c')
elif 'status' in a:
 count=2 if off else 3; print('H'); [print(f'x  y  z  q  leader') for _ in range(count)]
elif 'alarm' in a: print('H')
""")
        self._write_executable("kubectl", r"""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
s=Path('__FAKE_STATE__'); a=sys.argv[1:]; off=(s/'power-off').exists(); contained=(s/'contained').exists()
with (s/'calls.log').open('a') as h: h.write(' '.join(a)+'\n')
def node(name):
 ready=not(off and name=='node-a'); ann={}
 if contained and name=='node-a': ann['homelab.supermorphic.com/node-lifecycle']='{"schemaVersion":1,"kind":"abrupt-loss"}'
 return {'apiVersion':'v1','kind':'Node','metadata':{'name':name,'uid':'uid-'+name,'resourceVersion':'10','annotations':ann},'spec':{'unschedulable':contained and name=='node-a'},'status':{'allocatable':{'cpu':'4','memory':'8Gi','pods':'100'},'conditions':[{'type':'Ready','status':'True' if ready else 'False'},{'type':'MemoryPressure','status':'False'},{'type':'DiskPressure','status':'False'},{'type':'PIDPressure','status':'False'}]}}
if 'replace' in a or 'create' in a:
 data=json.load(sys.stdin)
 if data.get('kind')=='Lease': (s/'lease.json').write_text(json.dumps(data))
 elif data.get('kind')=='Node':
  rec=data.get('metadata',{}).get('annotations',{}).get('homelab.supermorphic.com/node-lifecycle')
  (s/'contained').write_text('1') if rec else (s/'contained').unlink(missing_ok=True)
 print(json.dumps(data)); raise SystemExit
if 'get' in a and 'lease' in a:
 p=s/'lease.json'
 if not p.exists(): raise SystemExit(1)
 data=json.loads(p.read_text()); data.setdefault('metadata',{})['resourceVersion']='1'; print(json.dumps(data)); raise SystemExit
if 'get' in a and any(value.startswith('--raw') for value in a):
 print('ok' if any(value == '--raw=/readyz' for value in a) else json.dumps({'resources':[{'name':'pods/eviction','kind':'Eviction'}]})); raise SystemExit
if 'get' in a and 'nodes.longhorn.io' in a:
 following=a[a.index('nodes.longhorn.io')+1:]
 if following and not following[0].startswith('-'): print(json.dumps({'spec':{'allowScheduling':True,'evictionRequested':False}}))
 else: print(json.dumps({'items':[{'status':{'conditions':[{'type':'Ready','status':'True'}]}} for _ in range(3)]}))
 raise SystemExit
if 'get' in a and 'settings.longhorn.io' in a: print('block-if-contains-last-replica'); raise SystemExit
if 'get' in a and 'node' in a:
 name=a[a.index('node')+1]; data=node(name)
 out=' '.join(a)
 if 'jsonpath' in out:
  print(data['metadata']['uid'] if 'metadata.uid' in out else data['status']['conditions'][0]['status'])
 else: print(json.dumps(data))
 raise SystemExit
if 'get' in a and 'nodes' in a: print(json.dumps({'items':[node(x) for x in ('node-a','node-b','node-c')]})); raise SystemExit
if 'get' in a and 'pods' in a and 'k8s-app=cilium' in a:
 print(json.dumps({'items':[{'spec':{'nodeName':n},'status':{'conditions':[{'type':'Ready','status':'True'}]}} for n in ('node-b','node-c')]})); raise SystemExit
if 'get' in a and 'volumes.longhorn.io' in a: print(json.dumps({'items':[{'metadata':{'name':'volume-a'},'spec':{'numberOfReplicas':2},'status':{'state':'attached','robustness':'healthy'}}]})); raise SystemExit
if 'get' in a and 'replicas.longhorn.io' in a: print(json.dumps({'items':[{'spec':{'volumeName':'volume-a','nodeID':'node-a','failedAt':''}},{'spec':{'volumeName':'volume-a','nodeID':'node-b','failedAt':''}}]})); raise SystemExit
if 'get' in a and ('pods' in a or 'persistentvolumeclaims' in a or 'persistentvolumes' in a): print(json.dumps({'items':[]})); raise SystemExit
raise SystemExit(0)
""")

    def _request(self) -> Path:
        kube = self.root / "kubeconfig"
        talos = self.root / "talosconfig"
        kube.write_text("contexts:\n  - name: fixture\n", encoding="utf-8")
        talos.write_text("contexts:\n  fixture: {}\n", encoding="utf-8")
        request = self.root / "request.json"
        request.write_text(json.dumps({"talos_node": "node-a", "talos_kubeconfig": str(kube), "talos_kube_context": "fixture",
            "talos_talosconfig": str(talos), "talos_talos_context": "fixture", "talos_source_dir": str(self.source),
            "talos_source_revision": self.revision, "talos_confirmation": "remove-power:node-a:192.0.2.10",
            "talos_test_confirmation": "chaos:node-abrupt-loss", "talos_evidence_dir": str(self.root / "evidence")}), encoding="utf-8")
        return request

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update({"PATH": f"{self.bin}:{environment['PATH']}", "FAKE_STATE": str(self.state)})
        return environment

    def _repository_fixture(self) -> Path:
        fixture = self.root / "repository"
        shutil.copytree(
            ROOT,
            fixture,
            ignore=shutil.ignore_patterns(".git", ".tmp", ".venv", ".ansible", ".cache", "__pycache__"),
        )
        (fixture / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
        (fixture / ".ansible").symlink_to(ROOT / ".ansible", target_is_directory=True)
        gateway = fixture / "scripts/playbook.sh"
        gateway.write_text(
            gateway.read_text().replace("set -euo pipefail\n", 'set -euo pipefail\nexport PATH="$FAKE_BIN:$PATH"\n', 1),
            encoding="utf-8",
        )
        scenario = fixture / "roles/talos_lifecycle/files/scenarios/node_abrupt_loss.py"
        scenario.write_text(
            scenario.read_text(encoding="utf-8")
            .replace('bounded_seconds("NODE_ABRUPT_LOSS_TIMEOUT_SECONDS", 180, 600)',
                     'bounded_seconds("NODE_ABRUPT_LOSS_TIMEOUT_SECONDS", 2, 600)')
            .replace('bounded_seconds("NODE_ABRUPT_PASSIVE_SECONDS", 600, 1800)',
                     'bounded_seconds("NODE_ABRUPT_PASSIVE_SECONDS", 2, 1800)')
            .replace('bounded_seconds("NODE_ABRUPT_PROBE_SECONDS", 5, 30)',
                     'bounded_seconds("NODE_ABRUPT_PROBE_SECONDS", 1, 30)'),
            encoding="utf-8",
        )
        return fixture

    def _descendants(self, parent: int) -> list[tuple[int, str]]:
        rows: list[tuple[int, int, str]] = []
        output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,command="], text=True)
        for line in output.splitlines():
            fields = line.strip().split(maxsplit=2)
            if len(fields) == 3:
                rows.append((int(fields[0]), int(fields[1]), fields[2]))
        found: list[tuple[int, str]] = []
        parents = {parent}
        while parents:
            children = [(pid, command) for pid, ppid, command in rows if ppid in parents]
            found.extend(children)
            parents = {pid for pid, _ in children}
        return found

    def _processes_containing(self, marker: str) -> list[tuple[int, str]]:
        output = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
        found: list[tuple[int, str]] = []
        for line in output.splitlines():
            fields = line.strip().split(maxsplit=1)
            if len(fields) == 2 and marker in fields[1]:
                found.append((int(fields[0]), fields[1]))
        return found

    def _wait_child(self, pid: int, descriptor: int, deadline: float, output: bytearray) -> int | None:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], 0.2)
            if ready:
                try:
                    output.extend(os.read(descriptor, 4096))
                except OSError:
                    pass
            waited, value = os.waitpid(pid, os.WNOHANG)
            if waited:
                return os.waitstatus_to_exitcode(value)
        return None

    def _stop_child(self, pid: int) -> None:
        descendants = self._descendants(pid)
        groups: set[int] = set()
        for child, _ in descendants:
            try:
                groups.add(os.getpgid(child))
            except ProcessLookupError:
                pass
        try:
            os.killpg(pid, 15)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
        for group in groups:
            try:
                os.killpg(group, 15)
            except (ProcessLookupError, PermissionError):
                pass

    def test_non_tty_rejects_before_any_api_mutation(self) -> None:
        command = [str(shutil.which("mise")), "run", "playbook", "--", "talos", "abrupt-loss-test", "production", "-e", f"@{self.request}"]
        result = subprocess.run(command, cwd=ROOT, env=self._environment(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.state / "calls.log").exists())

    def test_real_gateway_check_has_no_api_mutation(self) -> None:
        repository = self._repository_fixture()
        value = json.loads(self.request.read_text())
        value.pop("talos_confirmation")
        value.pop("talos_test_confirmation")
        value.pop("talos_evidence_dir")
        request = self.root / "check-request.json"
        request.write_text(json.dumps(value), encoding="utf-8")
        command = [str(shutil.which("mise")), "run", "playbook", "--", "talos", "maintenance-check", "production",
                   "-e", f"@{request}", "--check"]
        environment = self._environment()
        environment["FAKE_BIN"] = str(self.bin)
        result = subprocess.run(command, cwd=repository, env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        calls = (self.state / "calls.log").read_text(encoding="utf-8")
        mutations = [line for line in calls.splitlines()
                     if any(f" {verb} " in f" {line} " for verb in
                            ("create", "replace", "patch", "delete", "cordon", "uncordon"))]
        self.assertEqual(mutations, [], calls)

    def test_real_gateway_ansible_runtime_and_scenario_keep_tty_and_lease_alive(self) -> None:
        repository = self._repository_fixture()
        command = [str(shutil.which("mise")), "run", "playbook", "--", "talos", "abrupt-loss-test", "production", "-e", f"@{self.request}"]
        pid, descriptor = pty.fork()
        if pid == 0:
            os.chdir(repository)
            environment = self._environment()
            environment["FAKE_BIN"] = str(self.bin)
            os.execve(command[0], command, environment)
        output = bytearray(); deadline = time.monotonic() + 45; removed = restored = False
        status = None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], 0.2)
            if ready:
                try: output.extend(os.read(descriptor, 4096))
                except OSError: pass
            text = output.decode(errors="replace")
            if not removed and "disconnect electrical input" in text:
                (self.state / "power-off").write_text("1")
                os.write(descriptor, b"\n"); removed = True
            if not restored and "Restore electrical input" in text:
                (self.state / "power-off").unlink(missing_ok=True)
                os.write(descriptor, b"\n"); restored = True
            waited, value = os.waitpid(pid, os.WNOHANG)
            if waited:
                status = os.waitstatus_to_exitcode(value); break
        if status is None:
            os.killpg(pid, 15)
            terminate_deadline = time.monotonic() + 2
            while time.monotonic() < terminate_deadline:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.killpg(pid, 9)
                except PermissionError:
                    os.kill(pid, 9)
                os.waitpid(pid, 0)
            self.fail(output.decode(errors="replace"))
        diagnostics = output.decode(errors="replace")
        for name in ("calls.log", "bridge.log"):
            path = self.state / name
            if path.exists():
                diagnostics += f"\n{name}:\n{path.read_text()}"
        self.assertEqual(status, 0, diagnostics)
        self.assertTrue(removed and restored)
        calls = (self.state / "calls.log").read_text()
        self.assertGreaterEqual(calls.count("replace --filename -"), 3)
        evidence = list((self.root / "evidence").glob("abrupt-loss-*/diagnostics/node-abrupt-loss-evidence.json"))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(json.loads(evidence[0].read_text())["phase"], "recovered")

    def test_signal_at_disruption_stops_gateway_owned_children(self) -> None:
        repository = self._repository_fixture()
        command = [str(shutil.which("mise")), "run", "playbook", "--", "talos", "abrupt-loss-test", "production", "-e", f"@{self.request}"]
        pid, descriptor = pty.fork()
        if pid == 0:
            os.chdir(repository)
            environment = self._environment()
            environment["FAKE_BIN"] = str(self.bin)
            os.execve(command[0], command, environment)
        output = bytearray()
        try:
            prompt_deadline = time.monotonic() + 30
            gateway_pid = None
            while time.monotonic() < prompt_deadline:
                ready, _, _ = select.select([descriptor], [], [], 0.2)
                if ready:
                    try:
                        output.extend(os.read(descriptor, 4096))
                    except OSError:
                        pass
                if b"disconnect electrical input" in output:
                    (self.state / "power-off").write_text("1")
                    os.write(descriptor, b"\n")
                    for child, command_line in self._descendants(pid):
                        if "scripts/talos_gateway.py" in command_line:
                            gateway_pid = child
                    if gateway_pid is not None and (self.state / "contained").exists():
                        break
            self.assertIsNotNone(gateway_pid, output.decode(errors="replace"))
            self.assertTrue((self.state / "contained").exists(), output.decode(errors="replace"))
            os.kill(gateway_pid, 15)
            restore_deadline = time.monotonic() + 10
            while time.monotonic() < restore_deadline:
                ready, _, _ = select.select([descriptor], [], [], 0.2)
                if ready:
                    try:
                        output.extend(os.read(descriptor, 4096))
                    except OSError:
                        pass
                if b"Restore electrical input" in output:
                    (self.state / "power-off").unlink(missing_ok=True)
                    os.write(descriptor, b"\n")
                    break
            status = self._wait_child(pid, descriptor, time.monotonic() + 10, output)
            self.assertIsNotNone(status, output.decode(errors="replace"))
            self.assertNotEqual(status, 0, output.decode(errors="replace"))
            leftovers = self._processes_containing(str(self.root))
            self.assertEqual(leftovers, [], output.decode(errors="replace"))
        finally:
            (self.state / "power-off").unlink(missing_ok=True)
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited = pid
            if not waited:
                self._stop_child(pid)


if __name__ == "__main__":
    unittest.main()
