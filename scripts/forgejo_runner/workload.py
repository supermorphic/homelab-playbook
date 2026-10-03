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


def validation_script(selector: str) -> str:
    if selector not in SCENARIOS:
        raise ValueError('Unknown Molecule workload selector')
    return '\n'.join([
        'mise trust --yes', 'mise install --locked', 'mise run bootstrap',
        'mise run test:molecule -- ' + shlex.quote(selector),
    ])


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


def workflow(label: str, application, repository: dict, workspace: Path, selector: str) -> str:
    import yaml
    clone = f"http://{application.app}:3000/{application.user}/{repository['name']}.git"
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
        validation_script(selector),
    ])
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


def build_image(experiment, descriptor: Path, architecture: str) -> str:
    import json
    import re
    from scripts.forgejo_runner.fixture import ROOT, load_candidate
    candidate = load_candidate(descriptor, architecture)
    try:
        mise = json.loads(descriptor.read_text())['mise_images'][architecture]
        if re.fullmatch(r'ghcr\.io/jdx/mise@sha256:[0-9a-f]{64}', mise) is None:
            raise ValueError('Invalid Mise image')
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Workload fixture requires an immutable Mise image') from error
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
