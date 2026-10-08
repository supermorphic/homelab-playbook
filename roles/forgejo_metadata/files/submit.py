#!/usr/bin/env python3
"""Root-only submission gateway for a fixed metadata control unit."""
import fcntl
import grp
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


def plan_labels(config=Path('/etc/forgejo-metadata/config.json'),runner=subprocess.run):
    from forgejo_metadata.model import load_config, positive
    with open(config) as stream: document=json.load(stream)
    mappings=load_config(document); timeout=document.get('timeout_seconds',600)
    if not positive(timeout) or timeout>1800: raise ValueError
    # The transient unit supplies credentials without granting the planner write access.
    properties=['User=svc-forgejo-metadata','Group=svc-forgejo-metadata',
        'WorkingDirectory=/usr/local/libexec/forgejo-metadata','UMask=0077',
        f'TimeoutStartSec={timeout+30}','MemoryMax=256M','TasksMax=32',
        'NoNewPrivileges=true','PrivateTmp=true','PrivateDevices=true',
        'ProtectSystem=strict','ProtectHome=true','ReadOnlyPaths=/var/lib/forgejo-metadata',
        'ProtectKernelTunables=true','ProtectKernelModules=true','ProtectControlGroups=true',
        'RestrictSUIDSGID=true','RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6','CapabilityBoundingSet=']
    for name in sorted({name for mapping in mappings for name in (mapping.source_credential,mapping.destination_credential)}):
        properties.append(f'LoadCredential={name}:/etc/forgejo-metadata/credentials/{name}')
    command=['systemd-run','--quiet','--wait','--collect','--pipe','--service-type=oneshot',
        '--unit=forgejo-metadata-labels-plan']+['--property='+value for value in properties]+[
        '/usr/bin/python3','-B','-m','forgejo_metadata','--config','/etc/forgejo-metadata/config.json','labels-plan']
    result=runner(command,check=False,capture_output=True,text=True,timeout=timeout+60,
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin'})
    if result.returncode: print('label_plan_failed'); return 1
    if len(result.stdout.encode())>1024*1024: raise ValueError
    value=json.loads(result.stdout)
    if not isinstance(value,dict) or not isinstance(value.get('mappings'),list) or not isinstance(value.get('digest'),str): raise ValueError
    print(json.dumps(value,sort_keys=True)); return 0


def submit(request,root=Path('/run/forgejo-metadata'),runner=subprocess.run):
    root=Path(root); published=None; fd=None
    try:
        fd=os.open(root/'control-submission.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077: return 1
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=root/'control.json'
        if path.exists() or path.is_symlink(): return 1
        temporary_fd,temporary=tempfile.mkstemp(dir=root,prefix='.control-')
        try:
            with os.fdopen(temporary_fd,'w') as stream:
                os.fchmod(stream.fileno(),0o640)
                if os.geteuid()==0: os.fchown(stream.fileno(),0,grp.getgrnam('svc-forgejo-metadata').gr_gid)
                json.dump(request,stream); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,path); published=path.stat().st_ino
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        result=runner(['systemctl','start','forgejo-metadata-control.service'],check=False,timeout=1900,
            env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin'})
        return 0 if result.returncode==0 else 1
    except (OSError,KeyError,ValueError,subprocess.TimeoutExpired): return 1
    finally:
        if published is not None:
            try:
                if not path.is_symlink() and path.stat().st_ino==published: path.unlink()
            except OSError: pass
        if fd is not None: os.close(fd)


def main():
    if os.geteuid()!=0: print('root_submission_required'); return 1
    try:
        if sys.argv[1:]==['labels-plan']: return plan_labels()
        if sys.argv[1:]: raise ValueError
        data=sys.stdin.buffer.read(16385)
        if len(data)>16384: raise ValueError
        request=json.loads(data)
        allowed={'operation','fingerprint','decision_ref','confirmation','previous_writers_stopped','outcomes_resolved','key','plan_digest'}
        if not isinstance(request,dict) or set(request)-allowed: raise ValueError
        return submit(request)
    except (ValueError,OSError,subprocess.TimeoutExpired): print('invalid_control_request'); return 1

if __name__=='__main__': raise SystemExit(main())
