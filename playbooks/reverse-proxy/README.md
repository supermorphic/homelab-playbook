# Shared private reverse proxy

These playbooks manage distribution-packaged Caddy as a host systemd service on
Debian 13. They target `reverse_proxy` through the
canonical `mise run playbook` gateway. Prerequisites are the
[OS baseline](../../docs/guides/managed-host-onboarding.md) and controller
[SOPS setup](../../docs/guides/sops-secrets.md).

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
client sources are independent of the SSH management-source list. Live values
belong in protected inventory. Activating routes also requires DNS pointing to
the selected private host address and trusted certificates. The explicit `infra` dependency can remain pending until the
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
The named PEM trust bundle is supplied by `reverse_proxy_trust_certificates`
in protected inventory. Trust names are immutable; trust rotation requires a
new name. See the [reverse-proxy guide](../../docs/guides/reverse-proxy.md) for examples
and the modem identity prerequisite. No TLS-verification bypass is available.

Provisioning commits a desired manifest together with Caddy's effective
configuration. Only explicitly deferred `infra` routes can wait for the first
certificate; other certificate failures stop activation. The TLS coordinator
uses that committed manifest and its verified private ingress binding when it
activates the first certificate. Protected host variables hold live inputs and
override the public group's empty defaults.

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
Backend ports and Caddy's administration socket are outside this monitoring
contract.
The edge check proves the HTTPS path responds; it does not prove application,
database, backup, or broader host health. Edge green with Semaphore red points
toward the application path. Both red points first toward DNS, networking, TLS,
or Caddy. Neither service depends on Gatus or Talos for operation or recovery.

The health-only route has exactly `hostname`, `certificate_name`, and
`health: true`. It cannot also declare a backend or customize the path or body.
Synthetic health-route example:

```yaml
- hostname: caddy.infra.example.com
  certificate_name: infra
  health: true
```

The [reverse-proxy guide](../../docs/guides/reverse-proxy.md#deploy-the-monitoring-endpoints)
covers private DNS, protected route enrollment, deployment, and consumer
verification. The selected URLs alone do not establish live deployment readiness.

The disposable proxy scenario stops only its fixture backend and proves that
its application probe fails while the edge still returns 200. It also checks
GET/HEAD, exact paths, unchanged application forwarding, idempotence, certificate
publication and renewal, rollback, and restart recovery. Offline evidence does
not establish deployed DNS or reachability from Talos.

## Update and recovery boundaries

Caddy is installed with `state: present` from Debian's distribution repository,
without a version pin or package hold. Existing OS maintenance owns upgrades;
`caddy upgrade` is outside the managed workflow. Daily updates follow Debian
Security policy.
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

## Documentation and validation

The [reverse-proxy guide](../../docs/guides/reverse-proxy.md) covers health-route
deployment, consumer handoff, troubleshooting, and recovery procedures.

[Specification 008](../../docs/specs/008-shared-private-reverse-proxy.md) records
the deployment decision, integration contracts, recovery design, and acceptance
requirements. Offline tests use disposable certificate and backend fixtures;
production network reachability, physical boot, and certificate issuance remain
separate operator evidence.
