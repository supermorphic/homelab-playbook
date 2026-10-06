#!/usr/bin/env python3
"""Exercise installed units and inspect independent fixture API effects."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import time
sys.path.insert(0,'/usr/local/libexec/forgejo-metadata')
from forgejo_metadata.model import load_config,enrollment_fingerprint

ROOT=Path('/var/lib/forgejo-metadata')
FIXTURE=Path('/etc/forgejo-metadata-fixture')
CONFIG=Path('/etc/forgejo-metadata/config.json')
ACCOUNT='svc-forgejo-metadata'
UNIT='forgejo-metadata.service'

def run(*args,success=True,stdin=None,timeout=45):
    result=subprocess.run(args,input=stdin,capture_output=True,text=True,timeout=timeout)
    assert 'synthetic-fixture-token' not in result.stdout+result.stderr
    if (result.returncode==0)!=success:
        journal_result=subprocess.run(['journalctl','-u',UNIT,'-u','forgejo-metadata-control.service','--no-pager','--output=cat','--lines=30'],capture_output=True,text=True,timeout=10)
        journal=journal_result.stdout+journal_result.stderr
        assert not any(value in journal for value in ('synthetic-fixture-token','Original text','Original comment')),'fixture data appeared in journal'
        settings=subprocess.run(['systemctl','show',UNIT,'forgejo-metadata-control.service','--property=Result,ExecMainCode,ExecMainStatus,StatusErrno','--no-pager'],capture_output=True,text=True,timeout=10).stdout
        raise AssertionError((args,result.stdout,result.stderr,journal,settings))
    return result.stdout.strip()

def start(success=True):
    # Reset burst counters only inside this owned fixture container. Inactive
    # units can be unloaded, so a reset naming a fresh unit can fail.
    run('systemctl','reset-failed')
    return run('systemctl','start',UNIT,success=success)

def control(operation,key=None):
    fingerprint=enrollment_fingerprint(load_config(json.loads(CONFIG.read_text())))
    value={'operation':operation,'fingerprint':fingerprint,'decision_ref':'disposable-systemd-acceptance',
        'confirmation':operation+':'+fingerprint,'previous_writers_stopped':True,'outcomes_resolved':True}
    if key: value['key']=key
    run('/usr/bin/python3','/usr/local/libexec/forgejo-metadata/submit.py',stdin=json.dumps(value))
    assert not Path('/run/forgejo-metadata/control.json').exists()

def snapshot():
    path=FIXTURE/'destination.json'
    return json.loads(path.read_text()) if path.exists() else {'issues':[],'labels':[],'milestones':[],'comments':[]}

def main():
    account=pwd.getpwnam(ACCOUNT)
    assert account.pw_uid==2105 and account.pw_gid==2105 and account.pw_shell=='/usr/sbin/nologin'
    assert os.getgrouplist(ACCOUNT,account.pw_gid)==[account.pw_gid]
    for path in [CONFIG,Path('/usr/local/libexec/forgejo-metadata/forgejo_metadata/cli.py')]:
        assert path.stat().st_uid==0 and not path.stat().st_mode & 0o022
    for path in Path('/etc/forgejo-metadata/credentials').iterdir():
        assert path.stat().st_uid==0 and stat.S_IMODE(path.stat().st_mode)==0o600
    settings={key:run('systemctl','show',UNIT,'--property='+key,'--value') for key in
        ('User','ProtectSystem','NoNewPrivileges','PrivateTmp','UMask','MemoryMax','TasksMax')}
    assert settings['User']==ACCOUNT and settings['ProtectSystem']=='strict' and settings['UMask']=='0077'
    assert settings['NoNewPrivileges']=='yes' and settings['PrivateTmp']=='yes'
    assert settings['MemoryMax']=='268435456' and settings['TasksMax']=='32',settings
    assert run('systemctl','show','forgejo-metadata.timer','--property=Persistent','--value')=='yes'
    assert '*:00/15:00' in Path('/etc/systemd/system/forgejo-metadata.timer').read_text()
    assert (ROOT/'state.json').read_bytes()==(FIXTURE/'state-baseline.json').read_bytes()
    assert json.loads((ROOT/'state.json').read_text())['pending']['fixture:7:issue:999']['key']['object_id']==999
    (ROOT/'state.json').unlink()
    start(success=False); assert not snapshot()['issues']; assert not (ROOT/'state.json').exists()
    # Both units must load their credentials to authenticate against the fixture APIs.
    control('bootstrap-initial'); control('manual-apply')
    data=snapshot(); assert len(data['issues'])==len(data['comments'])==len(data['labels'])==len(data['milestones'])==1
    assert data['issues'][0]['state']=='closed' and data['issues'][0]['labels']==['fj-bug-42'] and data['issues'][0]['milestone']==102
    assert 'original' in data['issues'][0]['body'] and 'commenter' in data['comments'][0]['body']
    before=(FIXTURE/'destination.json').read_bytes(); start(); assert before==(FIXTURE/'destination.json').read_bytes()
    # A service-owned lock blocks all runtime modes, including root-submitted controls.
    lock=ROOT/'operation.lock'; fd=os.open(lock,os.O_RDWR); fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try: start(success=False)
    finally: os.close(fd)
    assert before==(FIXTURE/'destination.json').read_bytes()
    # Loss of safety state cannot create; attended recovery rediscovers the same objects.
    (ROOT/'state.json').unlink(); start(success=False); assert before==(FIXTURE/'destination.json').read_bytes()
    control('bootstrap-recover'); start(); assert before==(FIXTURE/'destination.json').read_bytes()
    # A bounded runtime deadline interrupts a delayed API response.
    original=CONFIG.read_bytes(); document=json.loads(original); document['timeout_seconds']=1
    CONFIG.write_text(json.dumps(document)); (FIXTURE/'mode.json').write_text(json.dumps({'delay':5}))
    started=time.monotonic()
    try: start(success=False); assert time.monotonic()-started<10
    finally: CONFIG.write_bytes(original); (FIXTURE/'mode.json').unlink()
    start()
    # Observe a missed calendar event through Persistent=true, without changing the clock.
    drop=Path('/etc/systemd/system/forgejo-metadata.timer.d'); drop.mkdir(exist_ok=True)
    (drop/'fixture.conf').write_text('[Timer]\nOnCalendar=\nOnCalendar=*-*-* *:00/1:00\nRandomizedDelaySec=0\nAccuracySec=1s\n')
    stamp=Path('/var/lib/systemd/timers/stamp-forgejo-metadata.timer'); stamp.parent.mkdir(parents=True,exist_ok=True); stamp.touch()
    os.utime(stamp,(time.time()-120,time.time()-120)); run('systemctl','daemon-reload')
    previous=json.loads((ROOT/'status.json').read_text())['attempted_at']; run('systemctl','start','forgejo-metadata.timer')
    deadline=time.monotonic()+15
    try:
        while json.loads((ROOT/'status.json').read_text())['attempted_at']<=previous and time.monotonic()<deadline: time.sleep(.1)
        assert json.loads((ROOT/'status.json').read_text())['attempted_at']>previous,'persistent timer did not catch up'
    finally:
        run('systemctl','stop','forgejo-metadata.timer'); (drop/'fixture.conf').unlink(); run('systemctl','daemon-reload')
    journal=run('journalctl','-u',UNIT,'-u','forgejo-metadata-control.service','--no-pager','--output=cat')
    assert 'Original text' not in journal and 'Original comment' not in journal
    print('PASS: effective credentials, sandbox, locks, recovery, timeout and persistent timer')

def seed():
    from forgejo_metadata.state import StateStore
    from forgejo_metadata.model import SourceKey
    account=pwd.getpwnam(ACCOUNT)
    store=StateStore(ROOT/'state.json',enrollment_fingerprint(load_config(json.loads(CONFIG.read_text()))))
    store.bootstrap('initial','disposable-provision-preservation')
    store.begin_create(SourceKey('fixture',7,'issue',999))
    os.chown(store.path,account.pw_uid,account.pw_gid)
    (FIXTURE/'state-baseline.json').write_bytes(store.path.read_bytes())

if __name__=='__main__':
    if sys.argv[1:]==['seed']: seed()
    else: main()
