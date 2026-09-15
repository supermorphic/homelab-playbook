"""Load the role's assertions as tasks in a disposable controller playbook file."""

from copy import deepcopy
from pathlib import Path
import sys

import yaml


role, destination = map(Path, sys.argv[1:])
tasks = []
for checkpoint in ("filesystem-preflight.yml", "configure.yml"):
    source = yaml.safe_load((role / "tasks" / checkpoint).read_text())
    assertions = [task for task in source if "ansible.builtin.assert" in task][:3]
    assert len(assertions) == 3
    for group, assertion in enumerate(assertions):
        probe = deepcopy(assertion)
        probe.update({
            "name": f"Observe existing {checkpoint} assertion group {group}",
            "vars": {"item": {
                "item": "{{ reverse_proxy_metadata_cases[reverse_proxy_metadata_index].logical }}",
                "stat": "{{ reverse_proxy_metadata_batch.results[reverse_proxy_metadata_index].stat }}",
            }},
            "loop": "{{ range(reverse_proxy_metadata_cases | length) | list }}",
            "loop_control": {"loop_var": "reverse_proxy_metadata_index"},
            "register": "reverse_proxy_metadata_decisions",
            "ignore_errors": True,
        })
        tasks.append(probe)
        tasks.append({
            "name": f"Require independent expected decisions for {checkpoint} group {group}",
            "ansible.builtin.assert": {"that": [
                "(reverse_proxy_metadata_decisions.results[item] is succeeded) "
                f"== reverse_proxy_metadata_cases[item].want[{group}]",
            ]},
            "loop": "{{ range(reverse_proxy_metadata_cases | length) | list }}",
        })
destination.write_text(yaml.safe_dump(tasks, sort_keys=False))
