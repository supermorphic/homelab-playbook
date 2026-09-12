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

## Update and recovery boundaries

Install Caddy with `state: present` from Debian's distribution repository. Do
not pin its version, hold the package, or run `caddy upgrade`. Existing OS
maintenance upgrades packages. Daily updates follow Debian Security policy.
Full OS maintenance includes ordinary package updates. These policies
do not promise automatic installation of every upstream Caddy release.

Binary upgrades may restart Caddy and briefly interrupt every route;
configuration rollback is not package rollback.

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
