"""Run a real repository candidate in the synthetic Forgejo worker."""

import base64
import os
from pathlib import Path
import shlex
import subprocess

from scripts.molecule import SCENARIOS


def workload_lock(root: Path):
    from scripts.molecule import invocation_lock, SubprocessCommandRunner
    return invocation_lock(root, SubprocessCommandRunner())


STOCK_RUNTIME_COMMANDS = (
    'mise run test:forgejo -- compatibility', 'mise run test:forgejo -- fixture',
    'mise run test:semaphore -- compatibility', 'mise run test:semaphore -- fixture',
    'mise run test:semaphore -- controller',
)


def validation_script(selector: str | None = None, *, suite=None) -> str:
    if suite is not None:
        if selector is not None or suite != 'stock-runtime':
            raise ValueError('Unknown or conflicting workload suite')
        commands = STOCK_RUNTIME_COMMANDS
    else:
        if selector not in SCENARIOS:
            raise ValueError('Unknown Molecule workload selector')
        commands = ('mise run test:molecule -- ' + shlex.quote(selector),)
    return '\n'.join(['mise trust --yes', 'mise install --locked', 'mise run bootstrap', *commands])


def registry_sources(selector=None, *, suite=None):
    validation_script(selector, suite=suite)
    if selector:
        return {platform.base_image for platform in SCENARIOS[selector].platforms}
    from scripts.forgejo.runtime import defaults
    from scripts.forgejo.fixture import SAMBA_IMAGE as forgejo_samba
    from scripts.semaphore.fixture import SAMBA_IMAGE as semaphore_samba
    from scripts.semaphore.test import SEMAPHORE_IMAGE, POSTGRES_IMAGE, RCLONE_IMAGE
    from scripts.semaphore import restore
    pins = defaults()
    return {pins['forgejo_image'], pins['forgejo_postgres_image'], pins['forgejo_rclone_image'],
            forgejo_samba, semaphore_samba, SEMAPHORE_IMAGE, POSTGRES_IMAGE, RCLONE_IMAGE,
            restore.SEMAPHORE_IMAGE, restore.POSTGRES_IMAGE, restore.RCLONE_IMAGE}


def restore_source_tree(root: Path, expected: str) -> str:
    """Recreate only archived objects; verify modes and content before seeding."""
    import re
    if re.fullmatch(r'[0-9a-f]{40}', expected) is None or (root / '.git').exists():
        raise ValueError('Invalid archived source candidate')
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='core.hooksPath',
                       GIT_CONFIG_VALUE_0=os.devnull, GIT_CONFIG_KEY_1='core.autocrlf',
                       GIT_CONFIG_VALUE_1='false')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args],
                                       env=environment, text=True, stderr=subprocess.PIPE).strip()
    git('init', '-q', '--object-format=sha1')
    git('add', '--force', '--all')
    actual = git('write-tree')
    if actual != expected:
        raise ValueError('Archived source does not match its declared tree')
    return actual


def source_tree(root: Path) -> str:
    changed = subprocess.run(['git', '-C', str(root), 'diff', '--exit-code', '--quiet'],
                             check=False, capture_output=True)
    if changed.returncode:
        raise RuntimeError('Stage the complete candidate before running the workload fixture')
    return subprocess.run(['git', '-C', str(root), 'write-tree'], check=True,
                          capture_output=True, text=True).stdout.strip()


def seed_repository(root: Path, application, repository: dict) -> str:
    """Push only the staged tree; never transfer local Git config or operator files."""
    tree = source_tree(root)
    environment = os.environ.copy()
    # Synthetic credentials never enter argv, repository config, jobs or logs.
    environment.update({
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_COUNT': '3',
        'GIT_CONFIG_KEY_0': 'credential.helper', 'GIT_CONFIG_VALUE_0': '',
        'GIT_CONFIG_KEY_1': 'core.hooksPath', 'GIT_CONFIG_VALUE_1': os.devnull,
        'GIT_CONFIG_KEY_2': 'http.extraHeader',
        'GIT_CONFIG_VALUE_2': 'Authorization: Basic ' + base64.b64encode(
            f'{application.user}:{application.password}'.encode()).decode(),
        'GIT_AUTHOR_NAME': 'Runner fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.com',
        'GIT_COMMITTER_NAME': 'Runner fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.com',
    })
    commit = subprocess.run(['git', '-C', str(root), 'commit-tree', tree], env=environment,
                            input='Synthetic workload candidate\n', text=True,
                            capture_output=True, check=True).stdout.strip()
    target = f"{application.url}/{application.user}/{repository['name']}.git"
    result = subprocess.run(['git', '-C', str(root), 'push', target,
                             f'{commit}:refs/heads/main'], env=environment,
                            text=True, capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise RuntimeError('Could not seed the disposable Forgejo repository')
    return tree


def require_success(rows: list[dict], commit: str) -> None:
    if len([r for r in rows if r.get('status') == 'success' and r.get('head_sha') == commit]) != 1:
        raise RuntimeError('Workload did not succeed at its exact workflow commit')


def runtime_workflow(label: str, workspace: Path, *, public_probe=False) -> str:
    """One allowlisted job exercises real remote API operations and loopback HTTP."""
    import yaml
    probe = '''import json, os, pathlib, subprocess, time
from http.client import HTTPConnection
def command(*args):
    result = subprocess.run(['podman', '--remote', *args], capture_output=True,
                            text=True, check=False, timeout=60)
    if result.returncode:
        raise RuntimeError('Job API operation failed: ' + result.stderr[-1024:])
    return result.stdout
info = json.loads(command('info', '--format=json'))
assert info['host']['security']['rootless'] is True
assert info['host']['cgroupVersion'] == 'v2'
workspace = pathlib.Path(os.environ['TMPDIR'])
build = workspace / 'build'; build.mkdir()
(build / 'Containerfile').write_text('FROM localhost/worker-synthetic:fixture\\nUSER 1:1\\n')
command('build', '--pull=never', '--tag', 'localhost/job-sibling:fixture', str(build))
share = workspace / 'share'; share.mkdir(); share.chmod(0o777)
server = "from http.server import BaseHTTPRequestHandler,HTTPServer; import pathlib; pathlib.Path('/work/share/sentinel').write_text('actual-job-sibling'); Handler=type('Handler',(BaseHTTPRequestHandler,),{'do_GET':lambda s:(s.send_response(200),s.end_headers(),s.wfile.write(b'actual-job-loopback')),'log_message':lambda *a:None}); HTTPServer(('0.0.0.0',8080),Handler).serve_forever()"
identity = command('run', '-d', '--name=actual-job-sibling', '--security-opt=no-new-privileges',
                   '--publish=127.0.0.1::8080', '-v', '/usr:/usr:ro,nosuid,nodev',
                   '-v', str(share) + ':/work/share:U', 'localhost/job-sibling:fixture',
                   '/usr/bin/python3', '-c', server).strip()
inspection = json.loads(command('inspect', identity))[0]
bindings = inspection['NetworkSettings']['Ports']
assert set(bindings) == {'8080/tcp'} and len(bindings['8080/tcp']) == 1
binding = bindings['8080/tcp'][0]
assert binding['HostIp'] == '127.0.0.1' and 1024 <= int(binding['HostPort']) <= 65535
deadline = time.monotonic() + 15
while True:
    connection = HTTPConnection('127.0.0.1', int(binding['HostPort']), timeout=2)
    try:
        connection.request('GET', '/health')
        response = connection.getresponse()
        assert response.status == 200 and response.read(64) == b'actual-job-loopback'
        break
    except OSError:
        if time.monotonic() >= deadline: raise
        time.sleep(0.1)
    finally:
        connection.close()
assert command('exec', identity, '/usr/bin/cat', '/work/share/sentinel').strip() == 'actual-job-sibling'
command('cp', identity + ':/work/share/sentinel', str(workspace / 'copied'))
assert (workspace / 'copied').read_text() == 'actual-job-sibling'
assert (share / 'sentinel').read_text() == 'actual-job-sibling'
command('rm', '--force', identity)
assert subprocess.run(['podman', '--remote', 'container', 'exists', identity]).returncode == 1
print('actual-job-runtime-passed')
'''
    script = "set -eu\npython3 - <<'PY'\n" + probe + 'PY\n'
    if public_probe:
        script += 'python3 "$TMPDIR/public-probe.py"\n'
    return yaml.safe_dump({'name': 'native runtime probe', 'on': ['push'], 'jobs': {
        'first': {'runs-on': label, 'steps': [{'env': {
            'CONTAINER_HOST': 'unix:///var/run/docker.sock', 'TMPDIR': str(workspace),
        }, 'run': script}]},
        'second': {'runs-on': label, 'steps': [{'run': 'echo unexpected-second-job'}]},
    }}, sort_keys=False)


def workflow(label: str, application, repository: dict, workspace: Path, selector: str | None = None,
             *, suite=None, clone_base=None, public_probe=False) -> str:
    import yaml
    base = clone_base or f"http://{application.app}:3000"
    clone = f"{base}/{application.user}/{repository['name']}.git"
    script = '\n'.join([
        'set -eu',
        'podman info --format json',
        'export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader',
        '''GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$ACTIONS_TOKEN" | base64 -w0)"''',
        'export GIT_CONFIG_VALUE_0',
        f'git clone {shlex.quote(clone)} {shlex.quote(str(workspace / "source"))}',
        'unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0 ACTIONS_TOKEN',
        f'cd {shlex.quote(str(workspace / "source"))}',
        'git checkout --detach "$CANDIDATE_SHA"',
        'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"',
        validation_script(selector, suite=suite),
    ])
    if public_probe:
        script += '\npython3 \"$TMPDIR/public-probe.py\"\n'
    return yaml.safe_dump({'name': 'repository workload', 'on': ['push'], 'jobs': {
        'first': {'runs-on': label, 'steps': [{'env': {
            'CANDIDATE_SHA': '${{ github.sha }}',
            'ACTIONS_TOKEN': '${{ github.token }}',
            'CONTAINER_HOST': 'unix:///var/run/docker.sock',
            'TMPDIR': str(workspace),
        }, 'run': script}]},
        'second': {'runs-on': label, 'steps': [{'run': 'echo unexpected-second-job'}]},
    }}, sort_keys=False)


def check_workload_available(experiment, selector: str) -> None:
    for platform in SCENARIOS[selector].platforms:
        if experiment.command([experiment.podman, 'container', 'exists', platform.container],
                              check=False).returncode != 1:
            raise RuntimeError('Selected Molecule fixture is already in use or cannot be inspected')


def mise_image(descriptor: Path, architecture: str) -> str:
    import json
    import re
    try:
        mise = json.loads(descriptor.read_text())['mise_images'][architecture]
        if re.fullmatch(r'ghcr\.io/jdx/mise@sha256:[0-9a-f]{64}', mise) is None:
            raise ValueError('Invalid Mise image')
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Workload fixture requires an immutable Mise image') from error
    return mise


def build_image(experiment, descriptor: Path, architecture: str) -> str:
    import json
    import re
    from scripts.forgejo_runner.fixture import ROOT, load_candidate
    candidate = load_candidate(descriptor, architecture)
    mise = mise_image(descriptor, architecture)
    name = 'localhost/' + experiment.name('job-image') + ':fixture'
    if experiment.command([experiment.podman, 'image', 'exists', name], check=False).returncode != 1:
        raise RuntimeError('Workload image already exists or cannot be inspected')
    experiment.command([
        experiment.podman, 'build', '--label', f'{experiment.label}={experiment.run_id}',
        '--build-arg', 'MISE_IMAGE=' + mise,
        '--build-arg', 'PODMAN_IMAGE=' + candidate['probe_image'], '--tag', name,
        '--file', str(ROOT / 'scripts/forgejo_runner/Containerfile.job'),
        str(ROOT / 'scripts/forgejo_runner'),
    ], timeout=900)
    experiment.job_images.append(name)
    return name
