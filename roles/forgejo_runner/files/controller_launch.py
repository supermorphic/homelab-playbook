"""One-job controller handoff with sealed, unnamed configuration credentials."""

import fcntl
import json
import os
import re
import sys
import urllib.parse


def runner_configuration(request):
    expected = {'url', 'label', 'job_image', 'worker_uid', 'controller_uid', 'controller_gid',
                'job_seconds', 'uuid', 'token', 'handle'}
    if not isinstance(request, dict) or set(request) != expected:
        raise ValueError('Invalid trusted controller request')
    url = urllib.parse.urlsplit(request['url'])
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('', '/')
            or not isinstance(request['label'], str)
            or re.fullmatch(r'[a-z][a-z0-9-]{0,47}', request['label']) is None
            or request['job_image'] != 'localhost/forgejo-job:approved'
            or any(type(request[key]) is not int or not 1000 <= request[key] < 2**32
                   for key in ('worker_uid', 'controller_uid', 'controller_gid'))
            or request['worker_uid'] == request['controller_uid']
            or type(request['job_seconds']) is not int or not 1 <= request['job_seconds'] <= 10800
            or any(not isinstance(request[key], str) or not 1 <= len(request[key]) <= 4096
                   or any(ord(character) < 32 or ord(character) == 127 for character in request[key])
                   for key in ('uuid', 'token', 'handle'))):
        raise ValueError('Invalid trusted controller configuration')
    return {'log': {'level': 'error'},
            'runner': {'capacity': 1, 'labels': [request['label'] + ':docker://' + request['job_image']],
                       'envs': {'CONTAINER_HOST': 'unix:///var/run/docker.sock',
                                'TMPDIR': '/work/job-workspace', 'FORGEJO_RUNNER_WORKSPACE': '/work/job-workspace'},
                       'timeout': str(request['job_seconds']) + 's'},
            'container': {'docker_host': 'unix:///run/user/' + str(request['worker_uid']) + '/podman.sock',
                          'workdir_parent': '/work/job-workspace',
                          'network': 'host', 'force_pull': False,
                          'options': '--tmpfs /var/lib/containers --tmpfs /home/podman/.local/share/containers '
                                     '--security-opt label=disable '
                                     '--volume /work/job-workspace:/work/job-workspace:U',
                          'valid_volumes': ['/work/job-workspace']},
            'cache': {'enabled': False},
            'server': {'connections': {'repository': {
                'url': request['url'], 'uuid': request['uuid'], 'token': request['token']}}}}


def runner_arguments(request, descriptor):
    runner_configuration(request)
    if type(descriptor) is not int or descriptor < 3:
        raise ValueError('Controller configuration requires a private inherited descriptor')
    return ['/usr/bin/setpriv', '--reuid=' + str(request['controller_uid']),
            '--regid=' + str(request['controller_gid']), '--clear-groups', '--no-new-privs',
            '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all',
            '/etc/runner-tools/forgejo-runner', '--config', '/proc/self/fd/' + str(descriptor),
            'one-job', '--handle', request['handle']]


def main():
    if os.geteuid() != 0:
        raise ValueError('Controller handoff requires trusted setup authority')
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        raise ValueError('Controller request exceeds its fixed bound')
    request = json.loads(raw)
    configuration = runner_configuration(request)
    # No credential is written to the worker filesystem, an argument or a log.
    # Kernel access to this descriptor follows the paired controller identity.
    descriptor = os.memfd_create('runner-config', os.MFD_ALLOW_SEALING)
    os.fchown(descriptor, request['controller_uid'], request['controller_gid'])
    os.fchmod(descriptor, 0o400)
    os.write(descriptor, json.dumps(configuration).encode())
    os.lseek(descriptor, 0, os.SEEK_SET)
    fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
    os.set_inheritable(descriptor, True)
    # Namespace and root descriptors belong only to trusted setup. The runner
    # retains its sealed configuration and standard streams after handoff.
    for name in os.listdir('/proc/self/fd'):
        number = int(name)
        if number > 2 and number != descriptor:
            try:
                os.close(number)
            except OSError:
                pass
    os.chdir('/controller')
    os.environ.clear()
    os.environ.update(PATH='/usr/sbin:/usr/bin:/sbin:/bin', HOME='/controller', TMPDIR='/controller', LC_ALL='C')
    arguments = runner_arguments(request, descriptor)
    os.execv(arguments[0], arguments)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Neither JSON input nor runner/API diagnostics are suitable public logs.
        raise SystemExit(1) from None
