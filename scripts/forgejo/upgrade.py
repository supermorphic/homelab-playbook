"""Real populated synthetic upgrade, historical recovery and Actions API rehearsal."""
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import time
import traceback
import zipfile

import yaml

from scripts.forgejo.runtime import ROOT, Run, defaults
from scripts.forgejo.shared import archive, backup, profiles, upgrade as transition, upgrade_host
from scripts.forgejo import fixture, restore
from scripts.forgejo.compatibility import temp_root
from scripts.forgejo_runner.registration_probe import ActionsApplication, runner_rows
from scripts.forgejo_runner.fixture import preflight
from scripts.forgejo_runner.workflow_probe import UPLOAD, start_job

SOURCE = profiles.REVIEWED['forgejo-15.0.9']
TARGET = profiles.REVIEWED['forgejo-16.0.6']
# Reviewed same-release runner fixtures; deployment platform/scope are unchanged.
RUNNER_IMAGES = {
    'amd64': 'data.forgejo.org/forgejo/runner@sha256:874c731a92850983e43f24dfe5a92a686092441324dd8844de28d3aac71b09fc',
    'arm64': 'data.forgejo.org/forgejo/runner@sha256:68fbd60b1198116506b4cb65c63c17c65b26548932131447bb1a8ad7afb1e774',
}
JOB_IMAGE = 'docker.io/library/node:24.18.0-bookworm@sha256:5711a0d445a1af54af9589066c646df387d1831a608226f4cd694fc59e745059'
LOG_MARKER = 'independent-upgrade-workflow-log'
ARTIFACT = b'independent upgrade artifact content\n'


def controller_digest():
    from scripts.forgejo.shared import DIRECTORY
    # The host recomputes this from the complete delivered immutable helper bundle.
    import importlib.util
    path = DIRECTORY / 'install.py'
    spec = importlib.util.spec_from_file_location('forgejo_install_fingerprint', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hashes = {name: hashlib.sha256((DIRECTORY / name).read_bytes()).hexdigest() for name in module.FILES}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def oracle_digest():
    paths = []
    for name in ('scripts/forgejo', 'scripts/forgejo_runner', 'roles/forgejo/files', 'roles/forgejo/templates',
                 'roles/forgejo_runner/files', 'roles/forgejo_runner/templates'):
        paths += [path for path in (ROOT / name).rglob('*') if path.is_file()
                  and path.suffix in ('.py', '.j2', '.sh')]
    paths += [ROOT / name for name in ('roles/forgejo/defaults/main.yml',
              'roles/forgejo_runner/defaults/main.yml', 'uv.lock', '.mise.toml')]
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def fingerprints():
    return controller_digest(), oracle_digest()


def evidence_record(controller, checks, cleanup_errors, *, oracle=None, current=None, error=None):
    outcomes = {name: checks.get(name) is True for name in transition.REQUIRED_CHECKS}
    outcomes['cleanup'] = not cleanup_errors
    return {'schema': 1, 'source': dict(SOURCE), 'target': dict(TARGET),
            'controller_sha256': controller, 'oracle_sha256': oracle, 'checks': outcomes,
            'cleanup_errors': list(cleanup_errors),
            'completed': error is None and all(outcomes.values())
                         and (current is None or current == (controller, oracle))}


def rows(document, name):
    values = document if isinstance(document, list) else document.get(name)
    if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
        raise RuntimeError('Actions API did not return its reviewed collection contract')
    return values


def source_workflow(experiment, root, source, client, token, runtime):
    prefix = f'/repos/{source.user}/recovery'
    label = experiment.name('upgrade-label')
    workflow = {'name': 'upgrade preservation', 'on': ['push'], 'jobs': {'preserved': {
        'runs-on': label, 'steps': [
            {'run': "printf '%s\\n' '" + LOG_MARKER + "'\nprintf '%s\\n' 'independent upgrade artifact content' > evidence.txt"},
            {'uses': UPLOAD, 'with': {'name': 'upgrade-preserved', 'path': 'evidence.txt',
                                    'if-no-files-found': 'error', 'retention-days': 1}},
        ]}}}
    result = source.request(prefix + '/contents/.forgejo/workflows/upgrade.yml', method='POST', payload={
        'content': base64.b64encode(yaml.safe_dump(workflow).encode()).decode(),
        'message': 'Seed independent upgrade workflow'})
    commit = result['commit']['sha']
    deadline = time.monotonic() + 180
    observed = None
    while time.monotonic() < deadline:
        observed = [run for run in rows(source.request(prefix + '/actions/runs?head_sha=' + commit), 'workflow_runs')
                    if run.get('commit_sha') == commit]
        if len(observed) == 1:
            break
        time.sleep(1)
    if not observed or len(observed) != 1:
        raise RuntimeError('source did not create exactly one synthetic workflow run')
    run_id = observed[0]['id']
    deadline = time.monotonic() + 60
    matching = []
    while time.monotonic() < deadline:
        queued = source.request(prefix + '/actions/runners/jobs?labels=' + label)
        matching = [job for job in queued if job.get('name') == 'preserved']
        if len(matching) == 1:
            break
        time.sleep(1)
    if len(matching) != 1 or not isinstance(matching[0].get('handle'), str):
        raise RuntimeError('source workflow did not expose one queued job handle')
    controller, registration = start_job(experiment, root, source, {'name': 'recovery'}, token.read_text(),
        runtime, {'runner_image': RUNNER_IMAGES[runtime['architecture']]}, JOB_IMAGE,
        label, matching[0]['handle'], 1)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        runs = rows(source.request(prefix + '/actions/runs?head_sha=' + commit), 'workflow_runs')
        selected = next((row for row in runs if row['id'] == run_id), None)
        if selected and selected.get('status') in ('success', 'failure', 'cancelled'):
            if selected['status'] != 'success':
                raise RuntimeError('source synthetic artifact workflow did not succeed')
            break
        time.sleep(2)
    else:
        raise RuntimeError('source artifact workflow exceeded its bounded budget')
    experiment.command([experiment.podman, 'wait', controller], timeout=60)
    remaining = runner_rows(source.url, prefix + '/actions/runners', token.read_text())
    if any(row['id'] == registration['id'] for row in remaining):
        source.request(prefix + '/actions/runners/' + str(registration['id']), method='DELETE')
    return {'run_id': run_id, 'commit': commit, 'workflow_blob': result['content']['sha']}


def download(client, path, name):
    if not path.startswith('/') or '..' in path or not name.startswith('/tmp/upgrade-'):
        raise ValueError('unsafe fixture download path')
    client.command(['curl', '--fail', '--silent', '--show-error', '--config', client.auth,
                    '--output', name, client.base + '/api/v1' + path])
    return base64.b64decode(client.command(['base64', name]).stdout)


def actions_acceptance(client, expected, workflow):
    prefix = f'/repos/{expected["username"]}/{expected["repository"]}'
    jobs = rows(client.request(prefix + f'/actions/runs/{workflow["run_id"]}/jobs'), 'jobs')
    selected = [job for job in jobs if job.get('name') == 'preserved']
    if len(selected) != 1:
        raise RuntimeError('migrated run lost its independently seeded job')
    job = selected[0]
    if job.get('run_id') != workflow['run_id'] or type(job.get('attempt')) is not int or job['attempt'] < 1:
        raise RuntimeError('migrated job/run/attempt identities are incomplete')
    log = download(client, prefix + f'/actions/jobs/{job["id"]}/logs', '/tmp/upgrade-job.log')
    if LOG_MARKER.encode() not in log:
        raise RuntimeError('job logs differ from the independent workflow marker')
    packed = download(client, prefix + f'/actions/runs/{workflow["run_id"]}/logs', '/tmp/upgrade-run.zip')
    with zipfile.ZipFile(io.BytesIO(packed)) as bundle:
        if not any(LOG_MARKER.encode() in bundle.read(name) for name in bundle.namelist() if not name.endswith('/')):
            raise RuntimeError('run log bundle lacks the independent workflow marker')
    artifacts = rows(client.request(prefix + f'/actions/runs/{workflow["run_id"]}/artifacts'), 'artifacts')
    artifact = [row for row in artifacts if row.get('name') == 'upgrade-preserved']
    if len(artifact) != 1 or artifact[0].get('run_id') != workflow['run_id']:
        raise RuntimeError('migrated run lost its associated artifact')
    packed = download(client, prefix + f'/actions/artifacts/{artifact[0]["id"]}/zip', '/tmp/upgrade-artifact.zip')
    with zipfile.ZipFile(io.BytesIO(packed)) as bundle:
        if bundle.read('evidence.txt') != ARTIFACT:
            raise RuntimeError('artifact contents differ from the independent byte oracle')
    all_artifacts = rows(client.request(prefix + '/actions/artifacts'), 'artifacts')
    if not any(row['id'] == artifact[0]['id'] for row in all_artifacts):
        raise RuntimeError('repository artifact listing does not include the migrated artifact')
    # Create a real queued disposable run, then cancel through the selected API.
    client.request(prefix + '/actions/workflows/upgrade.yml/dispatches', method='POST', payload={'ref': 'main'})
    deadline = time.monotonic() + 60
    queued = None
    while time.monotonic() < deadline:
        runs = rows(client.request(prefix + '/actions/runs'), 'workflow_runs')
        candidates = [row for row in runs if row['id'] != workflow['run_id'] and row.get('status') in ('waiting', 'blocked')]
        if candidates:
            queued = max(candidates, key=lambda row: row['id'])
            break
        time.sleep(1)
    if queued is None:
        raise RuntimeError('disposable cancellation workflow did not enter the queue')
    client.request(prefix + '/actions/runs/' + str(queued['id']) + '/cancel', method='POST')
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        runs = rows(client.request(prefix + '/actions/runs'), 'workflow_runs')
        if any(row['id'] == queued['id'] and row.get('status') == 'cancelled' for row in runs):
            break
        time.sleep(1)
    else:
        raise RuntimeError('API cancellation did not produce an observable cancelled run')
    capabilities = capability_probes(client, prefix)
    return {'run_id': workflow['run_id'], 'job_id': job['id'], 'attempt': job['attempt'],
            'capabilities': capabilities,
            'artifact_sha256': hashlib.sha256(ARTIFACT).hexdigest(), 'cancelled_run_id': queued['id']}


def api_status(client, path, payload):
    result = client.command(['curl', '--silent', '--show-error', '--max-time', '15',
        '--config', client.auth, '--request', 'POST', '--header', 'Content-Type: application/json',
        '--data-binary', '@-', '--output', '/tmp/capability-response.json', '--write-out', '%{http_code}',
        client.base + '/api/v1' + path], input_text=json.dumps(payload))
    return int(result.stdout)


def capability_probes(client, prefix):
    owner = prefix.split('/')[2]
    fork = client.request(prefix + '/forks', method='POST', payload={'name': 'upgrade-custom-fork'})
    fork_prefix = '/repos/' + owner + '/' + fork['name']
    branch = 'upgrade-fork-only'
    client.request(fork_prefix + '/branches', method='POST',
                   payload={'new_branch_name': branch, 'old_branch_name': 'main'})
    client.request(fork_prefix + '/contents/upgrade-fork-feature.txt', method='POST',
                   payload={'branch': branch, 'message': 'Synthetic fork capability probe',
                            'content': base64.b64encode(b'independent custom fork change\n').decode()})
    status = api_status(client, prefix + '/pulls',
                        {'base': 'main', 'head': owner + ':' + branch, 'title': 'Synthetic same-owner fork probe'})
    if status not in (201, 400, 404, 409, 422):
        raise RuntimeError('custom-fork probe returned an unexpected API failure')
    return {'same_owner_custom_fork_pr_status': status,
            'same_owner_custom_fork_pr_supported': status == 201,
            'rerun': 'no operation in the reviewed target OpenAPI schema'}


def check_host_runtime(experiment, root, archive_path, restored, expected):
    """Run the host's exact storage/user mappings, rather than restore mappings."""
    state = root / 'host-runtime'
    (state / 'credentials').mkdir(parents=True, mode=0o700)
    (state / 'database').mkdir(mode=0o700)
    shutil.copytree(restored / 'extracted/data', state / 'data')
    shutil.copytree(restored / 'extracted/config', state / 'config')
    config = state / 'config'
    (config / 'app.ini').write_text(upgrade_host.maintenance_config((config / 'app.ini').read_text()))
    password = (config / 'database-password').read_text()
    experiment.private_file(state / 'credentials', 'postgres-admin-password', password)
    experiment.private_file(state / 'credentials', 'postgres.env',
        'POSTGRES_USER=forgejo\nPOSTGRES_DB=forgejo\n'
        'POSTGRES_PASSWORD_FILE=/run/secrets/postgres-admin-password\n')
    identifier = '0' * 16 + experiment.run_id
    database = experiment.create('container', 'host-postgres',
        upgrade_host.database_arguments(SOURCE, state, identifier)[5:])
    experiment.wait([experiment.podman, 'exec', database, 'pg_isready', '-h', '127.0.0.1', '-U', 'forgejo'])
    backup.stream_command([experiment.podman, 'exec', '-i', database, 'pg_restore', '-U', 'forgejo',
        '-d', 'forgejo', '--exit-on-error', '--no-owner', '--no-acl'],
        source=archive_path / 'database.dump', timeout=300)
    arguments = upgrade_host.application_arguments(TARGET, state, config, identifier)[5:]
    arguments[arguments.index('--network') + 1] = 'container:' + database
    arguments[-1:-1] = ['--volume', str(restored / 'auth') + ':/test-auth:ro,z']
    application = experiment.create('container', 'host-app', arguments)
    experiment.wait([experiment.podman, 'exec', application, 'curl', '--fail', '--silent',
                     'http://127.0.0.1:3000/api/healthz'], timeout=120)
    client = restore.Client(experiment, application)
    client.auth_directory = restored / 'auth'
    restore.assert_expected(client, expected, write_branch='host-runtime-write')
    if {line.split(':', 1)[0].strip() for line in client.command(['cat', '/proc/net/dev']).stdout.splitlines()
            if ':' in line} != {'lo'}:
        raise RuntimeError('exact host runtime gained an external interface')


def run():
    tested_controller, tested_oracle = fingerprints()
    experiment, runtime = preflight()
    migrated = Run()
    recovered = Run()
    host_runtime = Run()
    checks = {}
    cleanup_errors = []
    body_completed = False
    error = None
    acceptance = None
    phase = 'source and workflow'
    try:
        with tempfile.TemporaryDirectory(prefix='upgrade-', dir=temp_root()) as temporary:
            root = Path(temporary)
            experiment.observe_volumes()
            runner = RUNNER_IMAGES[runtime['architecture']]
            for image in (runner, JOB_IMAGE, TARGET['image']):
                phase = 'pull reviewed fixture image ' + image.split('@')[0]
                experiment.command([experiment.podman, 'pull', image], timeout=600)
            auth = root / 'source-auth'
            auth.mkdir(mode=0o700)
            selected = defaults()
            selected.update({'forgejo_' + key: value for key, value in SOURCE.items()})
            phase = 'initialize source runtime'
            source = ActionsApplication(experiment, root, auth_dir=auth, runtime_pins=selected)
            phase = 'initialize source synthetic administrator'
            source.initialize_admin()
            phase = 'seed source application state'
            client, token = fixture.source_auth(experiment, source, root)
            expected = fixture.seed(source, client, token)
            phase = 'execute source artifact workflow'
            workflow = source_workflow(experiment, root, source, client, token, runtime)
            expected['refs'] = client.refs(client.base + f'/{source.user}/recovery.git')
            sentinel = fixture.Sentinel(experiment, source, client, root)
            phase = 'consistent source capture'
            capture = fixture.Capture(experiment, source, root)
            result = backup.perform_capture(capture)
            path = capture.backups / result['archive_id']
            complete = archive.completion(path)
            experiment.private_file(path, 'COMPLETE', json.dumps(complete))
            archive.validate_archive(path, True)
            settings = root / 'source-settings'
            settings.mkdir(mode=0o700)
            for name in restore.KEYS:
                experiment.private_file(settings, name, (source.config / name).read_text())
            experiment.private_file(settings, 'generation.json', json.dumps(
                {'schema': 1, 'recovery_generation': capture.settings['recovery_generation']}))
            expected_file = experiment.private_file(root, 'expected-state.json', json.dumps(expected))
            expected = restore.checked_expected(expected_file)
            options = restore.Options(archive_id=path.name, remote='nas:synthetic-upgrade',
                rclone_config=root / 'unused-rclone.conf', settings_dir=settings,
                expected_state=expected_file, destination=root / 'migration', run_id=migrated.run_id,
                confirm_restore_experiment=migrated.run_id)
            phase = 'source reconstruction before migration'
            restore.restore_local(migrated, path, options, expected)
            old_client = restore.Client(migrated, migrated.name('restore-app'))
            old_client.auth_directory = options.destination / 'auth'
            before = deepcopy(expected)
            before['refs'] = old_client.refs(old_client.base + f'/{source.user}/recovery.git')
            phase = 'exact host runtime mappings with populated database'
            check_host_runtime(host_runtime, root, path, options.destination, before)
            phase = 'actual target migration in isolated source storage'
            migrated.command([migrated.podman, 'stop', '--time', '30', old_client.container])
            app = migrated.create('container', 'upgraded-app', restore.application_arguments(
                migrated.name('restore-postgres'), options.destination / 'extracted/data',
                options.destination / 'extracted/config', options.destination / 'auth', TARGET['image']))
            migrated.wait([migrated.podman, 'exec', app, 'curl', '--fail', '--silent', 'http://127.0.0.1:3000/api/healthz'], timeout=120)
            checks['migration'] = True
            upgraded = restore.Client(migrated, app)
            upgraded.auth_directory = options.destination / 'auth'
            restore.assert_expected(upgraded, before, write_branch='upgraded-write')
            checks['continuity'] = True
            phase = 'genuine migrated Actions API acceptance'
            acceptance = actions_acceptance(upgraded, expected, workflow)
            checks['actions'] = True
            phase = 'independent original-source recovery after target migration'
            source_options = restore.Options(archive_id=path.name, remote='nas:synthetic-upgrade',
                rclone_config=root / 'unused-rclone.conf', settings_dir=settings,
                expected_state=expected_file, destination=root / 'source-recovery', run_id=recovered.run_id,
                confirm_restore_experiment=recovered.run_id)
            restore.restore_local(recovered, path, source_options, expected)
            sentinel.verify(recovered, expected)
            checks['source_restore'] = True
            interfaces = upgraded.command(['cat', '/proc/net/dev']).stdout
            if {line.split(':', 1)[0].strip() for line in interfaces.splitlines() if ':' in line} != {'lo'}:
                raise RuntimeError('migrated target gained an external interface')
            denied = upgraded.command(['curl', '--fail', '--silent', '--max-time', '2', 'http://203.0.113.1/'], check=False)
            if denied.returncode == 0:
                raise RuntimeError('migrated target reached an external route')
            checks['isolation'] = True
            phase = 'owned cleanup'
        body_completed = True
    except Exception:
        error = phase
        detail = ROOT / '.tmp/forgejo' / (experiment.run_id + '.upgrade-error.log')
        detail.parent.mkdir(parents=True, exist_ok=True)
        detail.write_text(traceback.format_exc())
        detail.chmod(0o600)
    finally:
        if error is None and not body_completed:
            error = phase
        cleanup_errors += recovered.cleanup()
        cleanup_errors += host_runtime.cleanup()
        cleanup_errors += migrated.cleanup()
        cleanup_errors += experiment.cleanup()
        current = fingerprints()
        if error is None and current != (tested_controller, tested_oracle):
            error = 'candidate changed during rehearsal'
        record = evidence_record(tested_controller, checks, cleanup_errors,
                                 oracle=tested_oracle, current=current, error=error)
        record.update(run_id=experiment.run_id, failure_phase=error, actions=acceptance)
        destination = ROOT / '.tmp/forgejo' / (experiment.run_id + '.upgrade.json')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
        destination.chmod(0o600)
        print('Upgrade rehearsal evidence: ' + str(destination.relative_to(ROOT)))
    if not record['completed'] or error is not None:
        print('Upgrade rehearsal failed at ' + str(error or 'cleanup'))
        return 1
    print('Populated source migration, retained Actions APIs and independent source recovery passed')
    return 0
