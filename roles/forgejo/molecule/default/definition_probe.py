"""Fixture-only publication; never starts the product container services."""
import json
from pathlib import Path
import sys

sys.path.insert(0, '/usr/local/libexec/forgejo')
import service

candidate = Path(sys.argv[1])
allocation = service.account_allocation()
with service.operation_lock(service.LOCK, 10):
    service.check_boundaries(allocation)
    service.validate_candidate(candidate, allocation)
    targets = service.targets_for(allocation)
    service.check_stable_inputs(candidate, targets)
    service.validate_generator(candidate, allocation)
    changed = service.publish_files(candidate, targets, allocation)
service.manager(['daemon-reload'])
print(json.dumps({'changed': changed}))
