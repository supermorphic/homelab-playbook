"""Fixed bridge between Publisher's journal and the existing Caddy Activator."""
import contextlib
import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from .publication import Publisher


RESTART_EXIT = 75
ADAPTER = '/usr/local/libexec/homelab-tls-caddy'


def activation_module():
    path = '/usr/local/libexec/homelab-reverse-proxy'
    loader = importlib.machinery.SourceFileLoader('homelab_proxy_activation', path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Integration:
    def __init__(self, activator):
        self.a = activator
        self.root = self.a.config_dir / 'tls/infra'
        # Disk recovery uses only Publisher's checked journal and pointer methods.
        # No certificate/network callbacks are available in the startup process.
        self.publisher = Publisher(self.root, self.a.tls_journal,
                                   self.a.ids[0], self.a.ids[3], None, None, None)

    def record(self):
        self.a.metadata(self.a.tls_journal.parent, stat.S_ISDIR,
                        self.a.ids[0], self.a.ids[1], 0o700)
        self.publisher._assert_state()
        return self.publisher._read_journal()

    def check_revision(self, binding):
        envelope, config = self.a.manifest().committed()
        if envelope['ingress']['manifest_sha256'] != binding['desired_revision']:
            raise ValueError('desired revision changed during TLS publication')
        return config

    def prepare(self, record):
        manifests = self.a.manifest()
        envelope, config = manifests.committed()
        import proxy_config
        return {'desired_revision': envelope['ingress']['manifest_sha256'],
                'previous_boot': self.a.boot.read_bytes().decode('utf-8'),
                'candidate_boot': proxy_config.render(config),
                'disk_recovery': None, 'restart': False,
                'activation_failed': False, 'restoration_failed': False}

    def recover_disk(self):
        record = self.record()
        if 'caddy' not in record:
            raise ValueError('TLS journal lacks Caddy rollback authority')
        binding = record['caddy']
        self.check_revision(binding)
        current = self.publisher._assert_state()
        if current not in (record['previous'], record['generation']):
            raise ValueError('TLS pointer differs from disk recovery authority')
        boot = self.a.boot.read_bytes().decode('utf-8')
        if boot not in (binding['previous_boot'], binding['candidate_boot']):
            raise ValueError('Caddy boot differs from disk recovery authority')
        candidate = record['status'] == 'recovered' and not binding['restart']
        selection = record['generation'] if candidate else record['previous']
        contents = binding['candidate_boot'] if candidate else binding['previous_boot']
        binding['disk_recovery'] = 'pending'
        self.publisher._write_journal(record)
        self.publisher._switch_current(selection)
        self.a.durable_write(self.a.boot, contents.encode(), 0o640, self.a.ids[3])
        binding['disk_recovery'] = 'restored'
        self.publisher._write_journal(record)

    def validate_candidate(self, path):
        path = Path(path)
        if path.parent != self.root:
            raise ValueError('candidate must be beneath fixed infra publication root')
        self.publisher._assert_generation(path.name)
        if os.path.lexists(self.a.pending):
            raise ValueError('Caddy configuration recovery is pending')
        manifests = self.a.manifest()
        envelope, config = manifests.committed()
        if os.path.lexists(self.a.tls_journal):
            record = self.record()
            self.check_revision(record['caddy'])
            if record['generation'] != path.name:
                raise ValueError('candidate differs from retained publication')
        restorecon = self.a.path('/usr/sbin/restorecon')
        if restorecon.exists():
            self.a.commands.run([str(restorecon), '-RF', str(path)])
        validation = self.a.config_dir / 'Caddyfile.tls-validation'
        host_path = Path('/etc/caddy/tls/infra') / path.name
        self.a.durable_write(validation, manifests.render(config, host_path).encode(), 0o640, self.a.ids[3])
        try:
            if restorecon.exists():
                self.a.commands.run([str(restorecon), '-F', str(validation)])
            self.a.validate(validation, certificate_generation=path)
        finally:
            validation.unlink()
            self.a.fsync_directory(self.a.config_dir)

    def activate(self, action):
        if action not in ('reload', 'deactivate'):
            raise ValueError('invalid fixed adapter action')
        if os.path.lexists(self.a.pending):
            raise ValueError('Caddy configuration recovery is pending')
        self.a.active()
        record = self.record() if os.path.lexists(self.a.tls_journal) else None
        if record is None:
            if action != 'reload':
                raise ValueError('deactivation requires publication rollback authority')
            manifests = self.a.manifest()
            unused, config = manifests.committed()
            if self.a.boot.read_bytes() != manifests.render(config).encode():
                raise ValueError('unjournaled configuration change is not permitted')
            desired = self.a.validate(self.a.boot)
            self.a.caddy('reload', self.a.boot)
            self.a.observed(desired)
            self.a.served_tls(desired)
            return
        binding = record['caddy']
        self.check_revision(binding)
        current = self.publisher._assert_state()
        if current not in (record['previous'], record['generation']):
            raise ValueError('selected generation differs from publication authority')
        rollback = current == record['previous']
        if action == 'deactivate' and (not rollback or current is not None):
            raise ValueError('deactivation requires absent previous generation')
        contents = binding['previous_boot'] if rollback else binding['candidate_boot']
        if self.a.boot.read_bytes().decode() not in (binding['previous_boot'], binding['candidate_boot']):
            raise ValueError('boot configuration differs from publication authority')
        self.a.durable_write(self.a.boot, contents.encode(), 0o640, self.a.ids[3])
        desired = self.a.validate(self.a.boot)
        if rollback:
            try:
                # Active JSON and exact listener set prove failed-first absence,
                # including when surviving routes share a default certificate.
                self.a.observed(desired)
                self.a.served_tls(desired)
            except Exception:
                try:
                    self.a.caddy('reload', self.a.boot)
                    self.a.observed(desired)
                    self.a.served_tls(desired)
                except Exception:
                    binding['restart'] = True
                    self.publisher._write_journal(record)
                    raise RestartNeeded('Caddy rollback requires restart') from None
            if binding['restart']:
                binding['restart'] = False
                self.publisher._write_journal(record)
        else:
            self.a.caddy('reload', self.a.boot)
            self.a.observed(desired)
            self.a.served_tls(desired)


class RestartNeeded(RuntimeError):
    """Return control to the parent, which owns the shared lock descriptor."""


class Deployment:
    """Parent-owned lock scope and bounded adapter/restart subprocesses."""
    def __init__(self, activator, policy):
        self._a = activator
        self.policy = policy

    @property
    def a(self):
        if self._a is None:
            self._a = activation_module().Activator()
        return self._a

    @property
    def integration(self):
        return Integration(self.a)

    def binding(self):
        if self.policy.reader_gid != self.a.ids[3]:
            raise ValueError('TLS reader differs from installed Caddy group')
        envelope, config = self.a.manifest().committed()
        actual = {(item['hostname'], item['address'], item['port']) for item in self.policy.endpoints}
        if (not self.a.manifest().endpoints(config) or actual != self.a.manifest().endpoints(config)
                or 'infra' not in config.get('deferred_certificates', [])):
            raise ValueError('TLS verification policy differs from desired managed endpoints')
        return envelope['ingress']['manifest_sha256']

    @contextlib.contextmanager
    def locked(self):
        with self.a.locked() as descriptor:
            saved = None
            if descriptor != 9:
                try:
                    saved = os.dup(9)
                except OSError:
                    pass
                os.dup2(descriptor, 9)
            try:
                self.binding()
                yield self
            finally:
                if descriptor != 9:
                    if saved is None:
                        os.close(9)
                    else:
                        os.dup2(saved, 9)
                        os.close(saved)

    def prepare(self, record):
        self.binding()
        return self.integration.prepare(record)

    def operation(self, action, path=None):
        with self.a.locked(inherited=True):
            revision = self.binding()
            argv = [ADAPTER, action] + ([] if path is None else [str(path)])
            result = subprocess.run(argv, pass_fds=(9,), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=None,
                                    cwd='/', timeout=900, check=False,
                                    env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'HOME': '/'})
            if result.returncode == RESTART_EXIT:
                self.restart(revision)
                # Startup restored disk authority. The same fixed action now
                # verifies restoration; it cannot silently activate another revision.
                result = subprocess.run(argv, pass_fds=(9,), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=None,
                                        cwd='/', timeout=900, check=False,
                                        env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'HOME': '/'})
            if result.returncode:
                raise RuntimeError('fixed Caddy integration operation failed')

    def restart(self, revision):
        publisher = self.integration.publisher
        expected = self.integration.record()
        if (not expected['caddy']['restart'] or expected['caddy']['desired_revision'] != revision
                or publisher._current_name() != expected['previous']):
            raise ValueError('restart lacks matching rollback authority')
        fcntl.flock(9, fcntl.LOCK_UN)
        try:
            self.a.commands.run(['/usr/bin/systemctl', 'restart', 'caddy.service'])
        finally:
            fcntl.flock(9, fcntl.LOCK_EX)
        with self.a.locked(inherited=True):
            actual = self.integration.record()
            comparable = dict(actual, caddy=dict(actual['caddy'],
                              disk_recovery=expected['caddy']['disk_recovery']))
            if (self.binding() != revision or comparable != expected
                    or publisher._current_name() != expected['previous']
                    or self.a.boot.read_bytes().decode() != expected['caddy']['previous_boot']
                    or actual['caddy']['disk_recovery'] != 'restored'):
                raise ValueError('publication changed during Caddy restart')
            configuration = self.a.validate(self.a.boot)
            self.a.observed(configuration)
            self.a.served_tls(configuration)


def main(arguments=None):
    args = list(sys.argv[1:] if arguments is None else arguments)
    if not (args in (['reload'], ['deactivate']) or len(args) == 2 and args[0] == 'validate'):
        print('usage: homelab-tls-caddy {validate PATH|reload|deactivate}', file=sys.stderr)
        return 2
    module = None
    try:
        if os.geteuid() != 0:
            raise ValueError('TLS integration requires root')
        module = activation_module()
        activator = module.Activator()
        with activator.locked(inherited=True):
            integration = Integration(activator)
            if args[0] == 'validate':
                integration.validate_candidate(Path(args[1]))
            else:
                integration.activate(args[0])
        return 0
    except RestartNeeded:
        return RESTART_EXIT
    except Exception as error:
        # ActivationError explicitly contains only safe, repository-owned
        # diagnostics. Other exceptions can contain protected input values.
        # HostCommands still discards raw Caddy and OpenSSL stderr.
        if module is not None and isinstance(error, module.ActivationError):
            print(f'TLS Caddy {args[0]} failed: {error}', file=sys.stderr)
        else:
            print('TLS Caddy integration failed; inspect private transaction state', file=sys.stderr)
        return 1
