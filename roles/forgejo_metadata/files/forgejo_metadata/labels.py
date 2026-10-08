"""Read-only exact-name adoption plans and explicitly attended label adoption."""
import copy
from dataclasses import asdict
import hashlib
import json
import time
from urllib.parse import quote
from .identity import parse_marker, render_projection
from .model import DestinationObject, MirrorError, Projection, SourceKey, enrollment_fingerprint, positive
from .reconcile import discover_owned, verify_projection
from .state import key_string


def digest(plan):
    content={key:value for key,value in plan.items() if key!='digest'}
    return hashlib.sha256(json.dumps(content,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def label_fields(row):
    return {'name':row['name'],'color':row['color'].lstrip('#').lower(),
            'description':row.get('description') or ''}


def build_adoption_plan(mappings,store,clients):
    plan={'fingerprint':enrollment_fingerprint(mappings),'mappings':[],
          'effect':'Adoption makes label name, color and description Forgejo-managed. Later changes also affect historical GitHub issues using that label.'}
    ownership=store.ownership_evidence()
    try: journal=store.load(allow_other_fingerprint=True)
    except MirrorError: journal={}
    pending=journal.get('pending',{}); retries=journal.get('retry_deadlines',{})
    for mapping in mappings:
        entry={key:getattr(mapping,key) for key in ('instance','source_origin','source_repo','source_id',
               'destination_origin','destination_repo','destination_id','actor_id','visibility')}
        entry.update(labels=[],errors=[]); plan['mappings'].append(entry)
        if max(retries.get(name,0) for name in ('*',mapping.source_credential,mapping.destination_credential))>time.time():
            raise MirrorError('rate_limited')
        source,destination=clients(mapping); destination.preflight(mapping)
        source_data=source.inventory(mapping); inventory=destination.inventory(mapping)
        if not source_data.complete or not inventory.complete: raise MirrorError('incomplete_inventory')
        try: owned=discover_owned(mapping,inventory,ownership)
        except MirrorError as error:
            entry['errors'].append(str(error)); owned={}
        for row in sorted(source_data.labels,key=lambda label:label['id']):
            key=SourceKey(mapping.instance,mapping.source_id,'label',row['id'])
            pair={'key':asdict(key),'source':label_fields(row),'destination':None,'projection':None,'status':'missing'}
            entry['labels'].append(pair)
            try: projection=render_projection(mapping,'label',row)
            except MirrorError as error:
                pair.update(status='unsupported',error=str(error)); continue
            pair['projection']=projection.fields
            matches=[label for label in inventory.labels if label['name'].casefold()==row['name'].casefold()]
            source_matches=[label for label in source_data.labels if label['name'].casefold()==row['name'].casefold()]
            target=owned.get(key)
            if len(matches)>1 or len(source_matches)>1:
                pair['status']='ambiguous'; continue
            if target:
                pair['destination']=dict(label_fields(target.fields),id=target.fields['id'])
                pair['status']='owned'
                if any(label['id']!=target.fields['id'] for label in matches): pair['status']='conflict'
                continue
            if not matches: continue
            candidate=matches[0]; pair['destination']=dict(label_fields(candidate),id=candidate['id'])
            # Equality alone is only a proposal. A timer never reaches this adoption path.
            if (not positive(candidate.get('id')) or pair['source']!=label_fields(candidate) or 'forgejo-mirror:' in pair['destination']['description']
                    or key_string(key) in pending):
                pair['status']='conflict'
            else: pair['status']='ready'
    plan['digest']=digest(plan)
    return plan


def adopt_labels(mappings,store,clients,review_digest,decision_ref,budget):
    store.require_initialized()
    if not isinstance(decision_ref,str) or not decision_ref.strip(): raise MirrorError('adoption_decision_required')
    # Reuse clients so their pacing and rate-limit state cover the whole operation.
    connections={mapping.instance+':'+str(mapping.source_id):clients(mapping) for mapping in mappings}
    def live_clients(mapping): return connections[mapping.instance+':'+str(mapping.source_id)]
    expected=build_adoption_plan(mappings,store,live_clients)
    if review_digest!=expected['digest']: raise MirrorError('adoption_plan_changed')
    if any(entry['errors'] or any(pair['status'] not in ('ready','owned','missing') for pair in entry['labels'])
           for entry in expected['mappings']): raise MirrorError('adoption_conflict')
    count=sum(pair['status']=='ready' for entry in expected['mappings'] for pair in entry['labels'])
    if count>budget: raise MirrorError('adoption_budget_exceeded')
    writes=0
    for mapping,entry in zip(mappings,expected['mappings']):
        if store.deferred(mapping): raise MirrorError('rate_limited')
        for pair in entry['labels']:
            if pair['status']!='ready': continue
            # Recollect all reviewed pairs, including the results of our preceding writes.
            current=build_adoption_plan(mappings,store,live_clients)
            if current['digest']!=digest(expected): raise MirrorError('adoption_plan_changed')
            source,destination=live_clients(mapping); destination.preflight(mapping)
            key=SourceKey(**pair['key']); candidate=pair['destination']
            locator='/labels/'+quote(candidate['name'],safe='')
            target=DestinationObject(key,locator,destination.read(locator))
            if dict(label_fields(target.fields),id=target.fields['id'])!=candidate:
                raise MirrorError('adoption_plan_changed')
            # Source markers are generated here; the operator only supplies the reviewed digest.
            projection=Projection(key,copy.deepcopy(pair['projection']))
            destination.update(target,projection); writes+=1
            observed=destination.read(locator)
            verify_projection(projection,observed)
            if observed.get('id')!=candidate['id'] or parse_marker(observed.get('description'),'label')!=key:
                raise MirrorError('ownership_conflict')
            store.remember_owned({key:DestinationObject(key,locator,observed)})
            pair.update(status='owned',destination=dict(label_fields(observed),id=observed['id']))
    # Existing marked labels also enter the journal on an attended repeat.
    for mapping in mappings:
        source,destination=live_clients(mapping); destination.preflight(mapping)
        owned=discover_owned(mapping,destination.inventory(mapping),store.load().get('ownership',{}))
        store.remember_owned(owned)
    store.record_adoption(review_digest,decision_ref,writes)
    return writes
