# Talos node lifecycle playbooks

These controller-only playbooks operate one established Talos control-plane
node through explicit Kubernetes and Talos API contexts. Production is the only
supported inventory. Talos nodes are not Ansible inventory hosts.

Use the canonical gateway with one absolute JSON or YAML input file:

```text
mise run playbook -- talos ACTION production -e @/absolute/private/request.json
```

Supported actions are `maintenance-check`, `maintenance-enter`,
`maintenance-exit`, `reboot`, and `abrupt-loss-test`. Only
`maintenance-check` accepts `--check`. The gateway rejects all other forwarded
Ansible options.

See the [Talos maintenance guide](../../docs/guides/talos-maintenance.md) for
request examples, source and chart-cache preparation, confirmations, physical
power steps, recovery, and evidence limits.
