"""Exercise observational validation of a loaded disposable systemd override."""
from pathlib import Path
import os
import sys

sys.path.insert(0, '/usr/local/libexec/forgejo')
import service

service.verify_effective_units()
directory = Path('/var/lib/svc-forgejo/.config/systemd/user/forgejo-backup.service.d')
override = directory / 'fixture-deadline.conf'
if directory.exists():
    raise RuntimeError('disposable override directory is already occupied')
directory.mkdir(mode=0o750)
os.chown(directory, 0, service.account_allocation()['gid'])
try:
    override.write_text('[Service]\nTimeoutStartSec=infinity\n')
    override.chmod(0o640)
    os.chown(override, 0, service.account_allocation()['gid'])
    service.manager(['daemon-reload'])
    before = override.read_bytes()
    try:
        service.verify_effective_units()
    except ValueError:
        pass
    else:
        raise RuntimeError('effective override was accepted')
    if override.read_bytes() != before:
        raise RuntimeError('observational verification modified the override')
finally:
    override.unlink(missing_ok=True)
    directory.rmdir()
    service.manager(['daemon-reload'])
service.verify_effective_units()
print('Effective unit drift rejected without repair; owned override removed')
