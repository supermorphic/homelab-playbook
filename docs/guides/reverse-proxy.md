# Reverse proxy deployment and recovery

These procedures operate the host Caddy service described in the
[reverse-proxy README](../../playbooks/reverse-proxy/README.md). Establish the
[OS baseline](managed-host-onboarding.md) and controller
[SOPS setup](sops-secrets.md) first. Production and staging playbook execution
requires explicit authorization for the exact action, inventory, host limit,
and extra arguments.

## Deploy the monitoring endpoints

The [README endpoint contract](../../playbooks/reverse-proxy/README.md#monitoring-endpoint-contract)
selects `https://caddy.infra.supermorphic.com/healthz` for the edge and
`https://semaphore.infra.supermorphic.com/api/ping` for Semaphore. The URLs alone
are not evidence of live deployment. Complete these steps before consumer
activation:

1. Create a private DNS record for `caddy.infra.supermorphic.com` pointing to the
   existing NUC4 private Caddy listener. Confirm the installed `infra` certificate
   covers that name; its existing wildcard needs no separate renewal workflow.
2. Through the protected inventory process, append the health route below.
   Use the selected real hostname in place of the synthetic example. Preserve
   all existing routes, including Semaphore's
   hostname, backend port, and certificate. Do not replace the authoritative list
   with only the health route. Retain the existing private bind addresses and
   approved client sources, and confirm the intended monitoring network is allowed.
3. With explicit authorization for that action and target, run
   `mise run playbook -- reverse-proxy provision production --limit nuc4`.
   This includes configuration validation and verification. The health route uses
   the same first-certificate deferral, reload, rollback, and boot recovery as
   other routes.
4. From the intended Talos monitoring network, use ordinary DNS and trusted HTTPS
   to request both URLs. For example, run
   `curl --fail --silent --show-error https://caddy.infra.supermorphic.com/healthz`
   and the equivalent request to Semaphore's `/api/ping`. Record HTTP 200 for each
   and `ok` for the edge. Do not use `--insecure`, an IP URL, or `--resolve` as
   consumer acceptance evidence. Never stop production Semaphore for this test.
5. Hand the two URLs and the deployment and network-verification results to
   homelab-talos#423 before activating the edge check. Homepage and Gatus changes
   belong in that repository; this repository does not generate their configuration.

Synthetic health-route declaration for the complete protected `reverse_proxy_routes`
list:

```yaml
- hostname: caddy.infra.example.com
  certificate_name: infra
  health: true
```

The health route uses the existing external certificate lifecycle; it does not
create a separate issuer or renewal schedule. Keep the Caddy administration
socket and application backend ports outside the monitoring interface.

## Troubleshooting and evidence

Use `systemctl status caddy` and `journalctl -u caddy` through an authorized host
session. Keep diagnostic output private when it contains deployment values.
After a failed activation, correct the reported configuration, ownership, or
certificate condition and rerun provisioning. Failed first activation retains
the admin-only configuration. If recovery fails, retain its diagnostic and
inspect the target through the independent administration path; do not delete
transaction records to bypass the failure.

If startup reaches the retry limit, inspect the Caddy journal and correct the
reported address, configuration, or certificate problem. Once corrected, an
authorized `systemctl reset-failed caddy.service` clears the start limit, and
`systemctl start caddy.service` attempts recovery immediately. Verify the proxy
and trusted HTTPS afterward. Reboot recovery does not require certificate renewal.

After package maintenance, run proxy verification and a trusted HTTPS check from
an approved client. Separately check denial from outside the allowed network.
Local verification does not establish DNS accuracy or network reachability.
Package rollback and lost-host reconstruction require the existing OS recovery
process and separately recoverable certificate material. Caddy caches and
application data are outside configuration recovery.
