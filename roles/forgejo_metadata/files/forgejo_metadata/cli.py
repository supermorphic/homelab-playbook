"""Bounded runtime and attended controls; messages contain error classes only."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import signal
import stat
import time
from .api import SourceAPI, DestinationAPI
from .model import MirrorError, SourceKey, load_config, enrollment_fingerprint, positive
from .reconcile import reconcile, discover_owned
from .state import StateStore, atomic_json, operation_lock, read_private


def trusted_request(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info=os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o027
                or info.st_uid==os.geteuid() or info.st_size>16384):
            raise MirrorError('untrusted_control_request')
        return json.load(stream)


def credential(name):
    directory=os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory: raise MirrorError('credentials_unavailable')
    fd=os.open(Path(directory)/name,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size>4096:
            raise MirrorError('unsafe_credential')
        token=stream.read().strip()
    if not token or any(c.isspace() for c in token): raise MirrorError('invalid_credential')
    return token


def clients(mapping):
    return SourceAPI(mapping,credential(mapping.source_credential)), DestinationAPI(mapping,credential(mapping.destination_credential))


def apply(mappings,store,root,budget,deadline_at):
    store.require_initialized()
    try: previous=read_private(root/'status.json')
    except (OSError,ValueError,MirrorError): previous={}
    if not isinstance(previous,dict) or previous.get('fingerprint')!=store.fingerprint or not isinstance(previous.get('mappings'),list): previous={}
    old={row['source']:row for row in previous.get('mappings',[]) if isinstance(row,dict) and 'source' in row}
    status={'fingerprint':store.fingerprint,'attempted_at':time.time(),'mappings':[]}; success=True
    remaining=budget; timed_out=False
    for mapping in mappings:
        identity=f'{mapping.instance}:{mapping.source_id}'
        last=old.get(identity,{}).get('last_converged')
        try:
            if timed_out or time.monotonic()>=deadline_at: raise MirrorError('run_timeout')
            if store.deferred(mapping): raise MirrorError('rate_limited')
            source,destination=clients(mapping)
            result=reconcile(mapping,source,destination,store,remaining)
            remaining-=result.writes
            timed_out='run_timeout' in result.errors
            errors=dict(Counter(result.errors)); backlog=result.backlog
            writes=result.writes; last=result.converged_at or last
            success=success and bool(result.converged_at)
        except (MirrorError,KeyError,TypeError,ValueError,AttributeError) as error:
            errors={str(error) if isinstance(error,MirrorError) else 'invalid_source_or_response':1}; backlog=0; writes=0; success=False
            timed_out=timed_out or 'run_timeout' in errors
        status['mappings'].append({'source':identity,'last_attempted':time.time(),
            'last_converged':last,'writes':writes,'backlog':backlog,'errors':errors})
    status['unresolved_intents']=len(store.load()['pending'])
    success=success and not status['unresolved_intents']
    atomic_json(root/'status.json',status)
    print('converged' if success else 'convergence_incomplete')
    return 0 if success else 1


def control(request,mappings,store,fingerprint):
    operations=('bootstrap-initial','bootstrap-recover','resolve-absent','manual-apply')
    operation=request.get('operation')
    if (operation not in operations or request.get('fingerprint')!=fingerprint
        or request.get('confirmation')!=f'{operation}:{fingerprint}'
        or not isinstance(request.get('decision_ref'),str) or not request['decision_ref'].strip()):
        raise MirrorError('invalid_control_confirmation')
    if operation=='manual-apply': return True
    if request.get('previous_writers_stopped') is not True or request.get('outcomes_resolved') is not True:
        raise MirrorError('recovery_decision_required')
    # Complete discovery must succeed for every enrollment before state changes.
    owned={}
    for mapping in mappings:
        source,destination=clients(mapping); destination.preflight(mapping)
        if not source.inventory(mapping).complete: raise MirrorError('incomplete_inventory')
        owned.update(discover_owned(mapping,destination.inventory(mapping)))
    if operation.startswith('bootstrap-'):
        store.bootstrap(operation.removeprefix('bootstrap-'),request['decision_ref'])
        for target in owned.values():
            store.finish_create(target.key,'created',target.locator)
    else:
        try: key=SourceKey(**request['key'])
        except (KeyError,TypeError): raise MirrorError('invalid_resolution_key') from None
        if (key.kind not in ('issue','comment','label','milestone') or not positive(key.object_id)
            or not any(key.instance==m.instance and key.repository_id==m.source_id for m in mappings)):
            raise MirrorError('invalid_resolution_key')
        if key in owned: store.finish_create(key,'created',owned[key].locator)
        else: store.resolve_absent(key,request['decision_ref'])
    print('control_completed'); return False


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    sub=parser.add_subparsers(dest='mode',required=True)
    sub.add_parser('plan',help='Print public enrollment and its confirmation fingerprint without changing state.')
    sub.add_parser('apply',help='Reconcile enrolled destination metadata (writes).')
    sub.add_parser('check',help='Observe initialization and convergence status without repairs.')
    attended=sub.add_parser('control',help='Execute a root-owned, attended request.')
    attended.add_argument('--request',required=True,help='Root-owned JSON: operation, fingerprint, decision_ref, confirmation=operation:fingerprint. Bootstrap and resolve require previous_writers_stopped=true and outcomes_resolved=true; resolve adds the exact SourceKey as key.')
    args=parser.parse_args(argv)
    previous_handler=None
    try:
        with open(args.config) as stream: document=json.load(stream)
        mappings=load_config(document); fingerprint=enrollment_fingerprint(mappings)
        if args.mode=='plan':
            print(json.dumps({'fingerprint':fingerprint,'mappings':[{'source':f'{m.instance}:{m.source_id}',
                'source_origin':m.source_origin,'source_repo':m.source_repo,'destination_origin':m.destination_origin,
                'destination_repo':m.destination_repo,'destination_id':m.destination_id,'visibility':m.visibility,'actor_id':m.actor_id} for m in mappings]},sort_keys=True))
            return 0
        root=Path(document['state_root']); info=root.lstat()
        if not root.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077:
            raise MirrorError('unsafe_state_directory')
        budget=document.get('write_budget',100); timeout=document.get('timeout_seconds',600)
        if not positive(budget) or budget>1000 or not positive(timeout) or timeout>1800: raise MirrorError('invalid_runtime_bounds')
        store=StateStore(root/'state.json',fingerprint)
        if args.mode=='check':
            store.require_initialized(); status=read_private(root/'status.json')
            freshness=document.get('freshness_seconds',3600)
            if not positive(freshness) or not isinstance(status,dict) or status.get('fingerprint')!=fingerprint: raise MirrorError('invalid_status')
            rows=status.get('mappings',[])
            if (not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows)
                or {row.get('source') for row in rows}!={f'{m.instance}:{m.source_id}' for m in mappings}
                or len(rows)!=len(mappings) or status.get('unresolved_intents') or store.load()['pending']
                or any(not row.get('last_converged') or time.time()-row['last_converged']>freshness or row.get('errors') or row.get('backlog') for row in rows)):
                raise MirrorError('convergence_pending')
            print('converged'); return 0
        def deadline(signum,frame): raise MirrorError('run_timeout')
        deadline_at=time.monotonic()+timeout
        previous_handler=signal.signal(signal.SIGALRM,deadline); signal.alarm(timeout)
        with operation_lock(root/'operation.lock'):
            if args.mode=='control':
                request=trusted_request(args.request)
                if not isinstance(request,dict): raise MirrorError('invalid_control_request')
                if not control(request,mappings,store,fingerprint): return 0
            return apply(mappings,store,root,budget,deadline_at)
    except MirrorError as error: print(str(error)); return 1
    except (OSError,ValueError,KeyError,TypeError): print('invalid_runtime_input_or_response'); return 1
    finally:
        if previous_handler is not None:
            signal.alarm(0); signal.signal(signal.SIGALRM,previous_handler)
