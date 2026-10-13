"""Bounded process-utility and orphan-reaping acceptance inside a real job."""

import inspect
import json
from pathlib import Path
import subprocess
import sys
import time


def require_reaped(pids, root=Path('/proc'), *, timeout=5):
    deadline = time.monotonic() + timeout
    while any((root / str(pid)).exists() for pid in pids):
        if time.monotonic() >= deadline:
            raise RuntimeError('Orphaned job children were not reaped')
        time.sleep(0.05)


def run():
    # The creator exits without waiting. Its children must be adopted and
    # reaped by the container init, independently of the workflow shell.
    creator = '''import json, os, time
pids = []
for _ in range(64):
    pid = os.fork()
    if pid == 0:
        time.sleep(0.1)
        os._exit(0)
    pids.append(pid)
print(json.dumps(pids), flush=True)
os._exit(0)
'''
    for iteration in range(2):
        created = subprocess.run([sys.executable, '-c', creator],
                                 capture_output=True, text=True, check=True, timeout=15)
        pids = json.loads(created.stdout)
        require_reaped(pids)
        rows = subprocess.run(['ps', '-eo', 'pid=,ppid=,stat=,comm='],
                              capture_output=True, text=True, check=True, timeout=5).stdout
        if not any(int(row.split()[0]) == 1 for row in rows.splitlines()):
            raise RuntimeError('Job ps did not observe its PID namespace')
        print(json.dumps({'pass': iteration + 1, 'reaped_children': len(pids)}))


def workflow(label):
    import yaml
    program = '\n'.join(['import json, subprocess, sys, time', 'from pathlib import Path',
                         inspect.getsource(require_reaped), inspect.getsource(run), 'run()'])
    return yaml.safe_dump({'name': 'job process acceptance', 'on': ['push'], 'jobs': {
        'first': {'runs-on': label, 'steps': [{'run': "python3 - <<'PY'\n" + program + '\nPY\n'}]},
        'second': {'runs-on': label, 'steps': [{'run': 'echo unexpected-second-job'}]},
    }}, sort_keys=False)


def fixture(descriptor):
    """Use the pinned amd64 deployment image and an ephemeral synthetic runner."""
    import tempfile
    from scripts.forgejo_runner.fixture import ROOT, cleaned_up, load_candidate, preflight
    from scripts.forgejo_runner.registration_probe import run_one_job, run_registration
    from scripts.forgejo_runner.workload import build_image
    experiment, runtime = preflight()
    if descriptor is None:
        raise ValueError('Process acceptance requires a reviewed immutable image descriptor')
    candidate = load_candidate(descriptor, 'amd64')
    experiment.observe_volumes()
    root = ROOT / '.tmp/forgejo-runner'
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='process-', dir=root) as directory, cleaned_up(experiment):
        directory = Path(directory)
        image = build_image(experiment, descriptor, 'amd64')
        identity = json.loads(experiment.command([experiment.podman, 'image', 'inspect', image]).stdout)[0]
        if identity['Architecture'] != 'amd64':
            raise ValueError('Process acceptance built the wrong image architecture')
        print('Managed job image: ' + identity['Id'], flush=True)
        candidate['probe_image'] = image
        application, repository, token = run_registration(experiment, directory)
        run_one_job(experiment, directory, application, repository, token,
                    runtime, candidate, process_probe=True)
    print('Job ps and two-pass orphan reaping passed; disposable resources removed')
    return 0
