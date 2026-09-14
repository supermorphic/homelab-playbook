# Shared private reverse proxy

These playbooks manage distribution-packaged Caddy as a host systemd service on
Debian 13. They target `reverse_proxy` through the
canonical `mise run playbook` gateway. Establish the
[OS baseline](../../docs/guides/managed-host-onboarding.md) and controller
[SOPS setup](../../docs/guides/sops-secrets.md) before using them.

- `provision.yml` installs the package and managed service configuration,
  reconciles the shared private firewall policy, activates declared routes, and
  verifies effective state.
- `verify.yml` observes the installed service, configuration, certificate
  consumption, and firewall state. It does not repair state or create test routes.

Both actions require the existing key-only `ansible` account and passwordless
sudo. Production and staging execution require separate explicit authorization
for the exact action, inventory, and arguments. Adding inventory membership or
passing `--check` does not supply that authorization.

## Usage

For an authorized target and action:

```bash
mise run playbook -- reverse-proxy provision production --limit nuc4
mise run playbook -- reverse-proxy verify production --limit nuc4
```

Provisioning includes verification. Standalone verification observes the existing
state. The gateway rejects password credential and task-selection overrides.

## Inputs and ownership

`reverse_proxy_bind_addresses`, `reverse_proxy_client_sources`, and
`reverse_proxy_routes` default to empty lists. With no routes, Caddy has only a
permission-protected Unix admin socket; no HTTPS listener or allowance exists.
Active routes require explicit private bind addresses and client CIDRs. HTTPS
client sources are independent of the SSH management-source list. Supply live
values through the existing protected inventory process. Activating routes also
requires DNS pointing to the selected private host address and trusted
certificates. The explicit `infra` dependency can remain pending until the
issue #5 issuer publishes its first certificate.

Synthetic configuration example:

```yaml
reverse_proxy_bind_addresses:
  - 10.20.30.40
reverse_proxy_client_sources:
  - 10.20.0.0/16
reverse_proxy_routes:
  - hostname: app.example.test
    backend_port: 18080
    certificate_name: app
```

Each local application route has an exact `hostname`, a `backend_port` from 1024
through 65535, and a safe `certificate_name`. For these routes, Caddy connects to
`127.0.0.1:<backend_port>`. Application roles own port allocation and loopback-only
publication under their separate Podman accounts. No socket-based discovery or
application account membership is required by Caddy.

Private device routes replace `backend_port` with an explicit `backend` mapping.
For example, an HTTP-only device uses:

```yaml
reverse_proxy_routes:
  - hostname: room-alert.infra.example.com
    certificate_name: infra
    backend:
      transport: http
      address: 10.20.30.60
      port: 80
reverse_proxy_deferred_certificates: [infra]
```

HTTPS device backends additionally require `server_name` and `trust_name`.
Supply the named PEM trust bundle through `reverse_proxy_trust_certificates`
in protected inventory. Trust names are immutable; use a new name to rotate
trust. See the [device guide](../../docs/guides/device-proxy.md) for examples
and the modem identity prerequisite. No TLS-verification bypass is available.

Provisioning commits a desired manifest together with Caddy's effective
configuration. Only explicitly deferred `infra` routes can wait for the first
certificate; other certificate failures stop activation. The TLS coordinator
uses that committed manifest and its verified private ingress binding when it
activates the first certificate. Declare live inputs in protected host variables
so they override the public group's empty defaults.

The complete desired route list is authoritative. Removing a route removes new
requests to that backend; removing the final route removes HTTPS listeners and
firewall allowances. It does not delete application data or external certificates.

The `caddy` account reads root-owned configuration and externally deployed TLS
files. The certificate owner supplies
`/etc/caddy/tls/<certificate_name>/current/fullchain.pem` and `privkey.pem`
through an atomic version-directory pointer. Certificate generation, renewal,
and issuer credentials belong to issue #5. The
[certificate contract](../../docs/specs/008-shared-private-reverse-proxy.md#external-certificate-contract)
defines ownership, permissions, version selection, and the shared activation lock.
Application authentication remains the application's responsibility; loopback
backends remain reachable by other local accounts.

## Monitoring endpoint contract

Issue #49 selects two independent HTTPS probes for the consumer in
[homelab-talos#423](https://github.com/supermorphic/homelab-talos/issues/423):

| Check | URL | Method and success | Response owner |
| --- | --- | --- | --- |
| Caddy edge | `https://caddy.infra.supermorphic.com/healthz` | GET: 200, body exactly `ok` (no newline); HEAD: 200, no body | Caddy static response |
| Semaphore | `https://semaphore.infra.supermorphic.com/api/ping` | Unauthenticated GET: 200 | Semaphore through its existing Caddy route |

Gatus sends GET every minute and requires HTTP 200. The edge response requires
no authentication and returns no host or configuration details. Only the exact
`/healthz` path accepts GET and HEAD; query strings do not change path matching.
Other paths and methods on the health hostname return 404. Application hostnames
retain their existing forwarding, including their own `/healthz` paths.

A dedicated hostname keeps the edge check stable when applications are added or
removed. It requires one private DNS record and reuses the existing `infra`
wildcard certificate and renewal workflow. Both checks use ordinary DNS,
hostname-verified TLS, and the existing source-restricted private TCP/443 ingress.
Do not expose a backend port or Caddy's administration socket for monitoring.
The edge check proves the HTTPS path responds; it does not prove application,
database, backup, or broader host health. Edge green with Semaphore red points
toward the application path. Both red points first toward DNS, networking, TLS,
or Caddy. Neither service depends on Gatus or Talos for operation or recovery.

The health-only route has exactly `hostname`, `certificate_name`, and
`health: true`. It cannot also declare a backend or customize the path or body.
Synthetic example to append to the complete protected route list:

```yaml
- hostname: caddy.infra.example.com
  certificate_name: infra
  health: true
```

### Deployment readiness and consumer handoff

The URL above is the selected contract, not evidence of live deployment.
Protected inventory, DNS, production provisioning, and the Talos-network check
remain operator steps:

1. Create a private DNS record for `caddy.infra.supermorphic.com` pointing to the
   existing NUC4 private Caddy listener. Confirm the installed `infra` certificate
   covers that name; its existing wildcard needs no separate renewal workflow.
2. Through the protected inventory process, append the route above using the
   selected real hostname. Preserve all existing routes, including Semaphore's
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

The disposable proxy scenario stops only its fixture backend and proves that
its application probe fails while the edge still returns 200. It also checks
GET/HEAD, exact paths, unchanged application forwarding, idempotence, certificate
publication and renewal, rollback, and restart recovery. Offline evidence does
not establish deployed DNS or reachability from Talos.

## Update and recovery boundaries

Install Caddy with `state: present` from Debian's distribution repository. Do
not pin its version, hold the package, or run `caddy upgrade`. Existing OS
maintenance upgrades packages. Daily updates follow Debian Security policy.
Full OS maintenance includes ordinary package updates. These policies
do not promise automatic installation of every upstream Caddy release.

Binary upgrades may restart Caddy and briefly interrupt every route;
configuration rollback is not package rollback.

Caddy requests and starts after `network-online.target`. Some network managers
reach that target before DHCP supplies every configured address. Failed starts
therefore retry every 10 seconds, with at most 12 starts in five minutes. A short
address delay can recover without operator action; repeated immediate failures
reach the start limit and remain failed. The limit is a rate limit, not a lifetime
retry count. Explicit service stops do not trigger retries.
Standalone verification checks this effective startup policy as well as activity.

Configuration changes validate under the service identity before reload and
preserve the committed boot configuration on failure. A root-owned transaction
record permits interrupted configuration recovery before systemd starts Caddy.
If a failed reload leaves extra listeners that runtime rollback cannot remove,
recovery restarts Caddy and can briefly interrupt every route. The helper checks
the restored configuration, listeners, and served certificates after restart.
Successful reloads can close WebSockets; applications must reconnect. The Debian
package does not support the newer WebSocket drain-delay option.

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

[Specification 008](../../docs/specs/008-shared-private-reverse-proxy.md) records
the deployment decision, integration contracts, recovery design, and acceptance
requirements. Offline tests use disposable certificate and backend fixtures;
production network reachability, physical boot, and certificate issuance remain
separate operator evidence.
