"""Fixture-only systemd stand-in using the production capture/recovery engine.

The separate probe user manager cannot start the installed product Quadlets.
It proves supervision and intent handling; stock PostgreSQL recovery is tested
by test:forgejo fixture, and physical reboot remains live operator evidence.
"""
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tarfile
import time

sys.path.insert(0, '/usr/local/libexec/forgejo')
import backup
import service

USER = 'svc-forgejo-probe'
STATE = Path('/var/lib/svc-forgejo-probe/recovery')
SCRIPT = '/usr/local/libexec/forgejo-recovery-probe.py'
APP = 'forgejo-probe-app.service'
CAPTURE = 'forgejo-probe-backup.service'
RECOVER = 'forgejo-probe-recover.service'
TRANSFER = 'forgejo-probe-transfer.service'


def command(args, *, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        raise RuntimeError('stand-in command failed: ' + args[0])
    return result


def manager(*args, check=True):
    options = ['--user']
    if os.geteuid() == 0:
        options += ['--machine=' + USER + '@.host']
    return command(['systemctl', *options, *args], check=check)


def user_command(*args, check=True):
    uid = pwd.getpwnam(USER).pw_uid
    return command(['runuser', '-u', USER, '--', 'env',
        f'XDG_RUNTIME_DIR=/run/user/{uid}',
        f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus',
        *args], check=check)


class Host:
    def __init__(self):
        self.state = STATE
        self.backups = STATE / 'backups'
        self.lock = service.LOCK
        self.lock_timeout = 1
        self.settings = json.loads((STATE / 'settings.json').read_text())
    def preflight(self):
        if not self.state.is_dir():
            raise RuntimeError('missing stand-in state')
    def active(self):
        return manager('is-active', APP, check=False).returncode == 0
    def stop(self):
        manager('stop', APP)
    def stopped(self):
        if self.active():
            raise RuntimeError('stand-in writers remain active')
    def dump(self, path):
        self.stopped()
        mode = (STATE / 'mode').read_text()
        if mode == 'restart-failure':
            (STATE / 'fail-start').write_text('fixture')
            raise RuntimeError('fixture capture failure')
        if mode in ('timeout', 'interrupt'):
            (STATE / 'capturing').write_text('fixture')
            time.sleep(30)
        path.write_bytes(b'PGDMP independent supervision fixture')
        path.chmod(0o600)
    def files(self, path):
        self.stopped()
        with tarfile.open(path, 'w:gz') as stream:
            content = b'independent supervision fixture\n'
            item = tarfile.TarInfo('data/sentinel')
            item.uid = item.gid = 1000
            item.mode = 0o600
            item.size = len(content)
            stream.addfile(item, io.BytesIO(content))
        path.chmod(0o600)
    def readable_dump(self, path):
        if not path.read_bytes().startswith(b'PGDMP'):
            raise RuntimeError('invalid stand-in payload')
    def start(self):
        manager('start', APP)
    def health(self):
        if not self.active():
            raise RuntimeError('stand-in did not recover')
    def trigger_transfer(self):
        manager('start', '--no-block', TRANSFER)


def eventually(predicate, message, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise RuntimeError(message)


def write_owned(path, content, mode=0o600):
    account = pwd.getpwnam(USER)
    service.atomic_write(path, content.encode(), mode=mode, uid=account.pw_uid, gid=account.pw_gid)


def mode(value):
    write_owned(STATE / 'mode', value)
    (STATE / 'capturing').unlink(missing_ok=True)


def recovered():
    return (not (STATE / 'restart-intent.json').exists()
            and manager('is-active', APP, check=False).returncode == 0)


def result(unit):
    return manager('show', unit, '--property=Result', '--value').stdout.strip()


def suite():
    account = pwd.getpwnam(USER)
    settings = json.loads(Path('/etc/containers/systemd/users/2202/.foundation-account.json').read_text())
    if account.pw_uid != 2203 or settings['name'] != 'svc-forgejo':
        raise RuntimeError('fixture account allocation differs')
    # Copy only public runtime pins from an actual role-generated Quadlet.
    quadlet = Path('/etc/containers/systemd/users/2202')
    pins = {}
    for key, filename in (('image', 'forgejo.container'), ('postgres_image', 'forgejo-postgres.container')):
        pins[key] = next(line[6:] for line in (quadlet / filename).read_text().splitlines() if line.startswith('Image='))
    # The transfer unit carries its immutable rclone image in the trusted helper manifest.
    # Read the fixture's explicitly supplied non-secret settings generated from defaults.
    pins['rclone_image'] = json.loads((STATE / 'pins.json').read_text())['rclone_image']
    pins['recovery_generation'] = 'a' * 32
    write_owned(STATE / 'settings.json', json.dumps(pins))
    backups = STATE / 'backups'
    backups.mkdir(mode=0o700, exist_ok=True)
    os.chown(backups, account.pw_uid, account.pw_gid)
    unit_root = Path('/var/lib/' + USER + '/.config/systemd/user')
    unit_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(unit_root.parent.parent, account.pw_uid, account.pw_gid)
    os.chown(unit_root.parent, account.pw_uid, account.pw_gid)
    os.chown(unit_root, account.pw_uid, account.pw_gid)
    units = {
        APP: f'[Service]\nType=simple\nExecStartPre=/usr/bin/python3 {SCRIPT} guard\nExecStart=/usr/bin/sleep infinity\nTimeoutStartSec=5\n',
        CAPTURE: f'[Unit]\nOnFailure={RECOVER}\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 {SCRIPT} capture\nExecStopPost=/usr/bin/systemctl --user --no-block start {RECOVER}\nTimeoutStartSec=3\nTimeoutStopSec=2\n',
        RECOVER: f'[Unit]\nConditionPathExists=!{STATE}/hold-recovery\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 {SCRIPT} recover\nTimeoutStartSec=5\nTimeoutStopSec=2\n[Install]\nWantedBy=default.target\n',
        TRANSFER: f'[Service]\nType=oneshot\nExecStart=/usr/bin/python3 {SCRIPT} transfer\nTimeoutStartSec=5\n',
    }
    for name, content in units.items():
        write_owned(unit_root / name, content, 0o600)
    manager('daemon-reload')
    manager('enable', RECOVER)
    mode('normal')
    manager('start', APP)
    for expected in (1, 2):
        manager('start', CAPTURE)
        eventually(recovered, 'consecutive capture did not recover')
        if len(list(backups.glob('forgejo-*'))) != expected or result(CAPTURE) != 'success':
            raise RuntimeError('consecutive capture did not publish independently')
        eventually(lambda: (STATE / 'transfers').exists() and int((STATE / 'transfers').read_text()) == expected,
                   'recovery did not trigger independent transfer')
        if manager('is-active', CAPTURE, check=False).stdout.strip() != 'inactive':
            raise RuntimeError('one-shot did not become inactive')
    mode('timeout')
    manager('start', CAPTURE, check=False)
    eventually(recovered, 'timed-out capture stranded availability')
    if result(CAPTURE) != 'timeout':
        raise RuntimeError('capture timeout was not observable')
    mode('interrupt')
    manager('reset-failed', CAPTURE)
    manager('start', '--no-block', CAPTURE)
    eventually(lambda: (STATE / 'capturing').exists(), 'capture did not stop the stand-in')
    manager('kill', '--signal=SIGKILL', '--kill-whom=all', CAPTURE)
    eventually(recovered, 'interrupted capture stranded availability')
    mode('restart-failure')
    manager('reset-failed', CAPTURE)
    manager('start', CAPTURE, check=False)
    eventually(lambda: (STATE / 'recovery-failed').exists(), 'restart failure was not retained')
    if recovered() or manager('start', APP, check=False).returncode == 0:
        raise RuntimeError('ordinary startup bypassed failed recovery')
    (STATE / 'fail-start').unlink()
    mode('normal')
    manager('reset-failed', APP, RECOVER, CAPTURE)
    manager('start', RECOVER)
    eventually(recovered, 'explicit recovery did not clear failure')
    manager('stop', APP)
    manager('start', CAPTURE)
    manager('start', RECOVER)
    if manager('is-active', APP, check=False).returncode == 0 or (STATE / 'restart-intent.json').exists():
        raise RuntimeError('backup started a deliberately stopped stand-in')
    manager('start', APP)
    # Exercise the same inode held by provision and capture, under real flock.
    before = len(list(backups.glob('forgejo-*')))
    with service.operation_lock(service.LOCK, 1):
        failed = user_command('python3', SCRIPT, 'capture', check=False)
        if failed.returncode == 0 or (STATE / 'restart-intent.json').exists():
            raise RuntimeError('contending capture bypassed the provision lock')
    if not recovered() or len(list(backups.glob('forgejo-*'))) != before:
        raise RuntimeError('lock contention changed application state')
    # Leave interrupted intent, then restart only the separate owned user manager.
    write_owned(STATE / 'hold-recovery', 'fixture')
    mode('interrupt')
    manager('start', '--no-block', CAPTURE)
    eventually(lambda: (STATE / 'capturing').exists(), 'restart fixture never began capture')
    manager('kill', '--signal=SIGKILL', '--kill-whom=all', CAPTURE)
    eventually(lambda: manager('is-active', CAPTURE, check=False).stdout.strip() == 'failed',
               'interrupted capture stayed active')
    command(['systemctl', 'stop', 'user@2203.service'])
    (STATE / 'hold-recovery').unlink()
    command(['systemctl', 'start', 'user@2203.service'])
    eventually(recovered, 'user-manager restart did not recover persisted intent')
    manager('stop', APP, CAPTURE, RECOVER, TRANSFER)
    manager('disable', RECOVER)
    print('Stand-in supervision passed: consecutive, timeout, interruption, failure, stopped intent, lock and manager restart')


def main():
    os.umask(0o077)
    action = sys.argv[1]
    if action == 'suite':
        if os.geteuid() != 0:
            raise RuntimeError('fixture suite requires administrator')
        suite()
        return
    if os.geteuid() != pwd.getpwnam(USER).pw_uid:
        raise RuntimeError('stand-in lifecycle requires the owned probe user')
    host = Host()
    if action == 'capture':
        with service.operation_lock(host.lock, host.lock_timeout):
            backup.capture_locked(host)
    elif action == 'recover':
        backup.recover(host)
    elif action == 'guard':
        backup.guard(host)
        if (STATE / 'fail-start').exists():
            raise RuntimeError('fixture restart failure')
    elif action == 'transfer':
        path = STATE / 'transfers'
        path.write_text(str(int(path.read_text()) + 1 if path.exists() else 1))
    else:
        raise RuntimeError('unknown stand-in action')


if __name__ == '__main__':
    main()
