"""Workflow evidence contracts for the disposable Forgejo experiment."""

import inspect
import base64
import json
from pathlib import Path
import re
import time

import yaml

from scripts.forgejo_runner.registration_probe import token_request

UPLOAD = 'https://code.forgejo.org/forgejo/upload-artifact@16871d9e8cfcf27ff31822cac382bbb5450f1e1e'
DOWNLOAD = 'https://code.forgejo.org/forgejo/download-artifact@d8d0a99033603453ad2255e58720b460a0555e1e'


def check_evidence(directory: Path, candidate: str, needs: dict) -> None:
    if re.fullmatch(r'[0-9a-f]{40}', candidate) is None:
        raise RuntimeError('Workflow candidate is invalid')
    expected = {'plan': 'success', 'matrix': 'success', 'required': 'success',
                'conditional': 'skipped'}
    if (not isinstance(needs, dict) or set(needs) != set(expected)
            or any(not isinstance(needs[name], dict) or needs[name].get('result') != status
                   for name, status in expected.items())):
        raise RuntimeError('Required workflow dependencies did not complete successfully')
    files = list(directory.glob('*.json'))
    if {path.name for path in files} != {'a.json', 'b.json'}:
        raise RuntimeError('Matrix evidence is incomplete or unexpected')
    for path in files:
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            raise RuntimeError('Matrix evidence is unreadable') from None
        if (not isinstance(row, dict) or row.get('child') != path.stem
                or row.get('candidate') != candidate or row.get('result') != 'success'):
            raise RuntimeError('Matrix evidence does not match the exact candidate and child')


def gate_script() -> str:
    return "python3 - <<'PY'\n" + '\n'.join([
        'import json, os, re', 'from pathlib import Path', inspect.getsource(check_evidence),
        'try:',
        "    check_evidence(Path(os.environ['EVIDENCE_DIRECTORY']), os.environ['CANDIDATE_SHA'],",
        "                   json.loads(os.environ['NEEDS_JSON']))",
        'except (RuntimeError, ValueError, KeyError):',
        "    raise SystemExit('Workflow evidence gate rejected the candidate')",
        "print('Complete exact-candidate workflow evidence accepted')", 'PY',
    ])


def require_result(observed: dict, tasks: list[dict], commit: str, case: str) -> None:
    expected_status = 'success' if case in ('complete', 'replacement') else (
        'cancelled' if case == 'cancelled' else 'failure')
    if observed.get('commit_sha') != commit or observed.get('status') != expected_status:
        raise RuntimeError('Workflow run did not match its expected exact-candidate outcome')
    matching = [row for row in tasks if row.get('head_sha') == commit]
    names = [row.get('name') for row in matching]
    if len(set(names)) != len(names):
        raise RuntimeError('Workflow tasks contained duplicate job evidence')
    statuses = {row['name']: row.get('status') for row in matching}
    if case == 'cancelled':
        if statuses.get('required') != 'cancelled' or statuses.get('gate') == 'success':
            raise RuntimeError('Cancelled required job unexpectedly satisfied its gate')
        return
    if case == 'replacement':
        expected = {'replacement': 'success'}
    elif case in ('complete', 'missing-child', 'wrong-candidate'):
        expected = {'plan': 'success', 'matrix-a (a)': 'success', 'required': 'success',
                    'gate': 'success' if case == 'complete' else 'failure'}
        if case != 'missing-child':
            expected['matrix-b (b)'] = 'success'
        # The task endpoint may omit a conditional job that never got a runner.
        if 'conditional' in statuses:
            expected['conditional'] = 'skipped'
    else:
        raise RuntimeError('Unknown synthetic workflow case')
    if statuses != expected:
        raise RuntimeError('Workflow jobs did not match the expected independent outcome')


def finished(observed: dict, tasks: list[dict], commit: str, case: str) -> bool:
    if observed.get('commit_sha') != commit or observed.get('status') not in ('success', 'failure', 'cancelled'):
        return False
    if case == 'cancelled':
        return observed['status'] == 'cancelled'
    final_job = 'replacement' if case == 'replacement' else 'gate'
    return any(row.get('head_sha') == commit and row.get('name') == final_job
               and row.get('status') in ('success', 'failure', 'cancelled', 'skipped', 'blocked')
               for row in tasks)


def workflow(label: str, case: str) -> str:
    if case == 'replacement':
        jobs = {'replacement': {'runs-on': label, 'steps': [{'run': 'echo cancellation-successor'}]}}
    else:
        matrix = {'child': ['a', 'b']}
        if case == 'missing-child':
            matrix['exclude'] = [{'child': 'b'}]
        evidence = '\n'.join([
            "python3 - <<'PY'", 'import json, os', 'from pathlib import Path',
            "child = os.environ['CHILD']", "candidate = os.environ['CANDIDATE_SHA']",
            "if os.environ['CASE'] == 'wrong-candidate' and child == 'b': candidate = 'b' * 40",
            "Path(child + '.json').write_text(json.dumps({'candidate': candidate,",
            "    'child': child, 'result': 'success'}))", 'PY',
        ])
        jobs = {
            'plan': {'runs-on': label, 'outputs': {'matrix': '${{ steps.plan.outputs.matrix }}'},
                     'steps': [{'id': 'plan', 'run': "printf '%s\\n' " +
                                "'matrix=" + json.dumps(matrix, separators=(',', ':')) +
                                "' >> \"$GITHUB_OUTPUT\""}]},
            'matrix': {'name': 'matrix-${{ matrix.child }}', 'runs-on': label, 'needs': ['plan'],
                       'strategy': {'fail-fast': False, 'matrix': '${{ fromJSON(needs.plan.outputs.matrix) }}'},
                       'steps': [{'env': {'CHILD': '${{ matrix.child }}', 'CASE': case,
                                          'CANDIDATE_SHA': '${{ forgejo.sha }}'}, 'run': evidence},
                                 {'uses': UPLOAD, 'with': {'name': 'result-${{ matrix.child }}',
                                  'path': '${{ matrix.child }}.json', 'if-no-files-found': 'error'}}]},
            'required': {'runs-on': label, 'needs': ['plan'],
                         'steps': [{'run': 'sleep 180' if case == 'cancelled' else 'echo required-success'}]},
            'conditional': {'runs-on': label, 'if': '${{ false }}',
                            'steps': [{'run': 'exit 99'}]},
            'gate': {'runs-on': label, 'needs': ['plan', 'matrix', 'required', 'conditional'],
                     'if': '${{ always() }}', 'steps': [
                         {'uses': DOWNLOAD, 'with': {'pattern': 'result-*', 'merge-multiple': 'true',
                                                    'path': 'evidence'}},
                         {'env': {'CANDIDATE_SHA': '${{ forgejo.sha }}', 'NEEDS_JSON': '${{ toJSON(needs) }}',
                                  'EVIDENCE_DIRECTORY': 'evidence'}, 'run': gate_script()}]},
        }
    return yaml.safe_dump({'name': 'workflow contract ' + case, 'on': ['push'],
                           'concurrency': {'group': 'synthetic-workflow-contract', 'cancel-in-progress': True},
                           'jobs': jobs}, sort_keys=False)


def publish(application, repository: dict, token: str, label: str, case: str, previous=None):
    path = f"/repos/{application.user}/{repository['name']}/contents/.forgejo/workflows/contract.yml"
    payload = {'content': base64.b64encode(workflow(label, case).encode()).decode(),
               'message': 'Synthetic workflow contract ' + case}
    if previous:
        payload['sha'] = previous['content']['sha']
    return token_request(application.url, path, token, method='PUT' if previous else 'POST', payload=payload)


def start_job(experiment, directory, application, repository, token, runtime, candidate,
              image: str, label: str, handle: str, sequence: int):
    path = f"/repos/{application.user}/{repository['name']}/actions/runners"
    registration = token_request(application.url, path, token, method='POST', payload={
        'name': experiment.name('workflow-' + str(sequence)), 'description': experiment.run_id,
        'ephemeral': True,
    })
    inputs = directory / ('workflow-' + str(sequence))
    inputs.mkdir(mode=0o700)
    data = inputs / 'data'
    data.mkdir(mode=0o700)
    config = {'runner': {'capacity': 1, 'timeout': '5m', 'labels': [f'{label}:docker://{image}']},
              'container': {'docker_host': 'unix://' + runtime['socket'], 'network': application.network,
                            'force_pull': False, 'options': '--security-opt label=disable --label ' +
                            experiment.label + '=' + experiment.run_id},
              'cache': {'enabled': False}, 'server': {'connections': {'fixture': {
                  'url': f'http://{application.app}:3000', 'uuid': registration['uuid'],
                  'token': registration['token']}}}}
    experiment.private_file(inputs, 'runner.yml', yaml.safe_dump(config))
    runner = experiment.create_controller('workflow-' + str(sequence), [
        '--network', application.network, '--userns=keep-id:uid=1000,gid=1000', '--user', '1000:1000',
        '--security-opt', 'label=disable', '--volume', f"{runtime['socket']}:{runtime['socket']}",
        '--volume', f'{inputs}:/fixture:ro', '--volume', f'{data}:/data',
        '--entrypoint', 'forgejo-runner', candidate['runner_image'],
        '--config', '/fixture/runner.yml', 'one-job', '--handle', handle,
    ])
    return runner, registration


def run(experiment, directory, application, repository, token, runtime, candidate, descriptor):
    from scripts.forgejo_runner.fixture import ROOT
    from scripts.forgejo_runner.workload import build_image
    image = build_image(experiment, descriptor, runtime['architecture'])
    experiment.command([experiment.podman, 'pull', candidate['runner_image']], timeout=300)
    path = f"/repos/{application.user}/{repository['name']}"
    label = experiment.name('workflow-label')
    evidence = ROOT / '.tmp/forgejo-runner'
    evidence.mkdir(parents=True, exist_ok=True)
    previous = None
    sequence = 0
    records = []
    for case in ('complete', 'missing-child', 'wrong-candidate', 'cancelled'):
        previous = publish(application, repository, token, label, case, previous)
        commit = previous['commit']['sha']
        active_commit = commit
        active_case = case
        cancelled_commit = None
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            response = token_request(application.url, path + '/actions/runs?head_sha=' + active_commit, token)
            runs = response.get('workflow_runs', [])
            matching = [row for row in runs if row.get('commit_sha') == active_commit]
            if len(matching) > 1:
                raise RuntimeError('Synthetic workflow created duplicate runs')
            tasks = token_request(application.url, path + '/actions/tasks?limit=50', token)['workflow_runs']
            if matching and finished(matching[0], tasks, active_commit, active_case):
                experiment.private_file(evidence, experiment.run_id + f'.{active_case}.tasks.json',
                    json.dumps({'commit': active_commit, 'status': matching[0]['status'],
                                'tasks': [{key: row.get(key) for key in ('id', 'name', 'head_sha', 'status')}
                                          for row in tasks]}, indent=2) + '\n')
                try:
                    require_result(matching[0], tasks, active_commit, active_case)
                except RuntimeError:
                    found = experiment.command([experiment.podman, 'exec', application.app, 'find',
                        '/var/lib/gitea', '-path', '*/actions_log/*', '-type', 'f'])
                    for index, logfile in enumerate(found.stdout.splitlines()[:50]):
                        content = experiment.command([experiment.podman, 'exec', application.app,
                                                      'base64', logfile])
                        experiment.private_file(evidence, experiment.run_id + f'.task-{index}.base64',
                                                content.stdout)
                    raise
                records.append({'case': active_case, 'commit': active_commit,
                                'status': matching[0]['status']})
                if cancelled_commit:
                    cancelled = token_request(application.url, path + '/actions/runs?head_sha=' + cancelled_commit,
                                              token)['workflow_runs']
                    if len(cancelled) != 1:
                        raise RuntimeError('Cancelled workflow did not retain one exact run')
                    require_result(cancelled[0], tasks, cancelled_commit, 'cancelled')
                    records.append({'case': 'cancelled', 'commit': cancelled_commit, 'status': 'cancelled'})
                print('Synthetic workflow contract passed: ' + case, flush=True)
                break
            queued = token_request(application.url, path + '/actions/runners/jobs?labels=' + label, token)
            if not queued:
                time.sleep(1)
                continue
            job = queued[0]
            sequence += 1
            runner, registration = start_job(experiment, directory, application, repository, token,
                runtime, candidate, image, label, job['handle'], sequence)
            if case == 'cancelled' and not cancelled_commit and job.get('name') == 'required':
                started = time.monotonic() + 30
                while time.monotonic() < started:
                    tasks = token_request(application.url, path + '/actions/tasks?limit=50', token)['workflow_runs']
                    if any(row.get('head_sha') == commit and row.get('name') == 'required'
                           and row.get('status') == 'running' for row in tasks):
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('Cancellation probe did not observe its running required job')
                previous = publish(application, repository, token, label, 'replacement', previous)
                cancelled_commit = commit
                active_commit = previous['commit']['sha']
                active_case = 'replacement'
            experiment.command([experiment.podman, 'wait', runner], timeout=360)
            logs = experiment.command([experiment.podman, 'logs', runner], check=False)
            experiment.private_file(evidence, experiment.run_id + f'.workflow-{sequence}.log',
                (logs.stdout + logs.stderr).replace(registration['token'], '[redacted]'))
            remaining = token_request(application.url, path + '/actions/runners?visible=false', token)
            if any(row.get('id') == registration['id'] for row in remaining):
                raise RuntimeError('Workflow job left its ephemeral runner registered')
        else:
            raise RuntimeError('Synthetic workflow did not reach its bounded terminal outcome')
    experiment.private_file(evidence, experiment.run_id + '.workflows.json',
                            json.dumps(records, indent=2) + '\n')
