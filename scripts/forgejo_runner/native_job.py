"""Real-job coordinator; all inputs and synthetic secrets stay in the worker."""

import json
import os
from pathlib import Path
import base64
import stat

from scripts.forgejo_runner.fixture import RunnerRun
from scripts.forgejo_runner.host_worker import validate_job_budget, observe_job_boundary


class NativeRun(RunnerRun):
    def __init__(self, images, endpoint, *, error_log=Path('/work/native-error.log')):
        super().__init__(podman='/usr/bin/podman')
        self.images = images
        self.endpoint = endpoint
        self.error_log = error_log

    def command(self, argv, **kwargs):
        def mapped(value):
            for prefix in ('MISE_IMAGE=', 'PODMAN_IMAGE='):
                if value.startswith(prefix):
                    source = value.removeprefix(prefix)
                    return prefix + self.images.get(source, source)
            return self.images.get(value, value)
        argv = [mapped(value) for value in argv]
        # Loading by the verified local filename preserves the exact OCI policy
        # identity. The remote upload API replaces it with a random temp path.
        if argv[0] == self.podman and argv[1] != 'load':
            argv = [argv[0], '--remote', '--url=unix:' + self.endpoint, *argv[1:]]
        check = kwargs.pop('check', True)
        result = super().command(argv, check=False, **kwargs)
        if check and result.returncode:
            fd = os.open(self.error_log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(result.stderr[:4096])
            raise RuntimeError('Native fixture backend exited ' + str(result.returncode))
        return result


def load_images(experiment, declared):
    images = {}
    for index, image in enumerate(declared):
        result = experiment.command([experiment.podman, 'load', '-i', '/work/input/' + image['asset']],
                                    timeout=180, check=False)
        if result.returncode:
            # No application or credential exists yet. Retain the useful public
            # asset error only in the existing private experiment diagnostic.
            raise RuntimeError('Offline image load failed: ' + result.stderr[-1024:])
        document = json.loads(experiment.command([experiment.podman, 'image', 'inspect', image['id']]).stdout)[0]
        if document['Id'].removeprefix('sha256:') != image['id'] or document['Architecture'] != 'amd64':
            raise ValueError('Native asset loaded the wrong image identity or architecture')
        name = f'localhost/native-image-{index}:fixture'
        experiment.command([experiment.podman, 'tag', image['id'], name])
        images[image['source']] = name
    return images


def save_job_diagnostic(run_id, directory, output):
    """Preserve only bounded logs; job failure remains the primary exception."""
    records = []
    for suffix in ('.runner.log', '.task-0.base64', '.task-1.base64'):
        try:
            fd = os.open(directory / (run_id + suffix), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    continue
                if suffix == '.runner.log':
                    stream.seek(max(0, metadata.st_size - 2048))
                    data = stream.read(2048)
                else:
                    if metadata.st_size > 1024**2:
                        continue
                    data = base64.b64decode(b''.join(stream.read(1024**2).split()), validate=True)
                    data = data[-3072:]
                records.append(data.decode(errors='replace'))
        except (OSError, ValueError, EOFError):
            continue
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write('\n'.join(records)[-4096:])


def seed_native_source(experiment, image, root, tree, application, repository):
    """Use job-image Git with only owned source and stdin synthetic credentials."""
    program = """import json,sys
from pathlib import Path
from types import SimpleNamespace
from scripts.forgejo_runner.workload import restore_source_tree,seed_repository
request=json.load(sys.stdin)
root=Path(request['root'])
restore_source_tree(root,request['tree'])
actual=seed_repository(root,SimpleNamespace(**request['application']),request['repository'])
print(actual)
"""
    request = {'root':str(root), 'tree':tree, 'repository':repository,
               'application':{key:getattr(application,key) for key in ('url','user','password')}}
    result = experiment.foreground('source-seed', [
        '--interactive', '--pull=never', '--network=host',
        '--userns=keep-id:uid=0,gid=0', '--user=0:0',
        '--volume', f'{root}:{root}', '--workdir', str(root),
        '--entrypoint', 'python3', image, '-B', '-c', program,
    ], input_text=json.dumps(request), timeout=180)
    if result.stdout.strip() != tree:
        raise ValueError('Seeded native workload does not match its declared tree')


def run_probe(*, observed=None):
    import offline_probe
    from scripts.forgejo_runner.registration_probe import run_registration, run_one_job
    if observed is None:
        observed = offline_probe.main()
    configuration = json.loads(Path('/job.json').read_text())
    selection = configuration.get('workload')
    if selection:
        from scripts.forgejo_runner.fixture import ROOT
        from scripts.forgejo_runner.workload import validation_script
        validation_script(**selection)
    worker = json.loads(Path('/worker.json').read_text())['worker']
    endpoint = f'/run/user/{worker["uid"]}/podman.sock'
    experiment = NativeRun({}, endpoint)
    images = load_images(experiment, configuration['images'])
    experiment.images = images
    directory = Path('/work/registration'); directory.mkdir(mode=0o700)
    experiment.observe_volumes()
    candidate = {key: images[value] for key, value in configuration['candidate'].items()}
    if selection:
        from scripts.forgejo_runner.workload import build_image
        descriptor = experiment.private_file(directory, 'candidate.json', json.dumps({
            'probe_images': {'amd64': configuration['candidate']['probe_image']},
            'runner_images': {'amd64': configuration['candidate']['runner_image']},
            'mise_images': {'amd64': configuration['mise_image']},
        }))
        job_image = build_image(experiment, descriptor, 'amd64')
        application, repository, token = run_registration(experiment, directory, empty=True)
        seed_native_source(experiment, job_image, ROOT, configuration['source_tree'], application, repository)
    else:
        # Namespace UID0 uses only the worker-owned socket, never host UID0.
        build = Path('/work/job-image'); build.mkdir()
        (build / 'Containerfile').write_text('FROM ' + candidate['probe_image'] + '\nUSER root\n')
        experiment.command([experiment.podman, 'build', '--pull=never', '--tag',
                            'localhost/native-job:fixture', str(build)], timeout=120)
        candidate['probe_image'] = 'localhost/native-job:fixture'
        application, repository, token = run_registration(experiment, directory)
    workspace = Path('/work/job-workspace'); workspace.mkdir(mode=0o700)
    public_probe = observed.get('public_https') is True
    if public_probe:
        import shutil
        from scripts.forgejo_runner.host_egress import FORGEJO_HOST
        certificates = workspace / 'public-ca.pem'
        shutil.copyfile('/etc/ssl/certs/ca-certificates.crt', certificates)
        source = Path('/egress_probe.py').read_text()
        function = source[source.index('def public_connections():'):source.index('\ndef main():')]
        function = function.replace("'/work/share/public-ca.pem'", repr(str(certificates)))
        (workspace / 'public-probe.py').write_text('import json,socket,ssl,urllib.request\nFORGEJO_HOST='
            + repr(FORGEJO_HOST) + '\n' + function + '\npublic_connections()\n')
    try:
        run_one_job(experiment, directory, application, repository, token,
                    {'socket': endpoint, 'offline': True, 'network': 'host',
                     'forgejo_url': application.url, 'workspace': workspace, 'public_probe': public_probe}, candidate,
                    workload={**selection, 'image': job_image, 'workspace': workspace,
                              'source_tree': configuration['source_tree']} if selection else None,
                    runtime_probe=not selection)
    except BaseException:
        from scripts.forgejo_runner.fixture import ROOT
        try:
            save_job_diagnostic(experiment.run_id, ROOT / '.tmp/forgejo-runner',
                                Path('/work/native-job-error.log'))
        except OSError:
            pass
        raise
    # Leave all creators, siblings and network helpers present for the host's
    # independent process/cgroup/namespace observation and whole-unit disposal.
    return {**observed, 'one_job': True, 'job_runtime': True,
            **({'job_workload': True} if selection else {}),
            **({'job_public_access': True} if public_probe else {})}
