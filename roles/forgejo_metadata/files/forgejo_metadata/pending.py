"""Read-only discovery of uncertain creates; reports never authorize a retry."""
from dataclasses import asdict
from .model import MirrorError, SourceKey, enrollment_fingerprint
from .reconcile import discover_owned


def build_pending_plan(mappings, store, clients):
    journal = store.load()
    report = {'fingerprint': enrollment_fingerprint(mappings), 'pending': [], 'observations': []}
    enrolled = {(mapping.instance, mapping.source_id) for mapping in mappings}
    for mapping in mappings:
        keys = [SourceKey(**intent['key']) for intent in journal['pending'].values()
                if intent['key']['instance'] == mapping.instance
                and intent['key']['repository_id'] == mapping.source_id]
        if not keys:
            continue
        if store.deferred(mapping):
            raise MirrorError('rate_limited')
        _, destination = clients(mapping)
        destination.preflight(mapping)
        inventory = destination.inventory(mapping)
        report['observations'].append({'source': f'{mapping.instance}:{mapping.source_id}',
                                       'requests': getattr(destination, 'requests', {})})
        conflict = False
        try:
            owned = discover_owned(mapping, inventory, journal.get('ownership', {}))
        except MirrorError as error:
            if str(error) != 'ownership_conflict':
                raise
            conflict = True
            owned = {}
        for key in sorted(keys, key=lambda item: (item.kind, item.object_id)):
            target = owned.get(key)
            entry = {'key': asdict(key),
                     'status': 'conflict' if conflict else 'found' if target else 'not_found'}
            if target is not None:
                entry['locator'] = target.locator
            report['pending'].append(entry)
    for intent in journal['pending'].values():
        key = intent['key']
        if (key['instance'], key['repository_id']) not in enrolled:
            report['pending'].append({'key': dict(key), 'status': 'not_enrolled'})
    return report
