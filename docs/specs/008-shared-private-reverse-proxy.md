# Specification 008: Shared private reverse proxy

Issue: [#25](https://github.com/supermorphic/homelab-playbook/issues/25)

## Purpose and deployment decision

Provide one private HTTPS entry point for explicitly declared off-cluster browser
services. Ansible owns configuration; systemd supervises a host Caddy service;
separate rootless Podman applications publish loopback HTTP backends. The target
uses the [OS baseline](003-os-maintenance-security-baseline.md).

A host service fits the small static service set: it can bind TCP/443 with one
service capability, read externally owned keys and reach separate applications
without container discovery, shared networks or additional port arrangements.
Do not add a Podman socket, shared application identity or public ingress.
Application authentication and backups belong to their service owners;
[specification 007](007-off-cluster-tls-trust.md) owns issuance and certificate
recovery. Workstation and console recovery must remain independent of the proxy.

Use the distribution Caddy package and existing OS update policy. Do not introduce
an upstream repository, binary installer, package hold or `caddy upgrade` workflow.
Validate capabilities rather than one exact installed release. Package upgrades
may briefly interrupt all routes; configuration rollback is not package rollback.

The dedicated non-login Caddy account receives only the privileged-port binding
capability and required private writable state. It has no login, sudo, application
groups or issuer credentials. Reconcile package-added supplementary groups before
startup as well as during provision. Reject incompatible identities or unmanaged
installations instead of taking them over. Configuration, keys, units and helpers
remain administrator-owned and unwritable by Caddy. Preserve sandboxing and the
baseline mandatory access controls.

## Declarative ingress and routes

The [role](../../roles/reverse_proxy/tasks/main.yml) and its
[validation source](../../roles/reverse_proxy/files/) own exact input schemas.
The complete desired route list is authoritative. Use exact hostnames, explicit
private listener addresses and explicit private client networks. HTTPS sources
are independently declared from SSH sources. Reject wildcard listeners, duplicate
hostnames, arbitrary fragments, traversal, URL credentials and unapproved upstreams.

Local application routes use host-loopback backends; their owners allocate ports
and configure publication. Loopback excludes remote clients but not other host
users. Device routes declare one private backend and transport. HTTPS device
routes require independently verified, route-specific trust and a matching server
name; HTTP routes accept no TLS trust settings. Trust names bind immutable content;
rotation uses a new name so rollback can retain the prior trust. Do not modify the
system trust store or disable backend verification. Device acceptance belongs to
specification 007.

Serve explicit external certificates on private TCP/443, with HTTP/1.1 and HTTP/2.
Disable automatic certificate management and redirects; create no TCP/80 or
UDP/443 listeners. Unknown names have no application route. Preserve normal HTTP
and WebSocket forwarding without trusting client-supplied proxy headers.
Successful reloads can close WebSockets; clients must reconnect. Failed validation
preserves the previous connections. Do not change package sources to add a newer
WebSocket drain option.

An empty desired list produces only the protected Unix administration socket,
with no network listener or firewall allowance. Removing routes removes access
without deleting application data or externally owned keys. Only explicitly
deferred first `infra` certificate routes may wait for certificate publication;
other missing or malformed certificates fail activation. Commit desired routes
and their verified ingress binding with the effective configuration so TLS does
not become a second routing or firewall controller.

Filesystem metadata observations are read-only, uncached and bounded to managed
paths. Do not reuse them across mutations or weaken independent ownership,
symlink, hard-link and content checks when batching observations.

## Firewall integration

The security baseline remains the only firewall policy owner. Compose the private,
source-scoped TCP/443 allowance with every other declared service extension so
later OS provisioning preserves approved routes. Read back runtime and permanent
rules, reuse guarded reconciliation and active-SSH-peer checks, and prove the
policy before exposing a new listener. Apply equivalent IPv4/IPv6 boundaries;
never open application or device backend ports as a proxy side effect.

## Independent HTTPS edge health

Use a dedicated hostname under the existing certificate namespace, for example
`caddy.infra.example.com`. Its static `/healthz` route has no application backend:
GET returns 200 with exactly `ok` and no newline; HEAD returns 200 without a body.
Query strings do not alter path matching. Other paths/methods return 404. The
health route uses the same private ingress, certificate deferral, transaction and
recovery guarantees as application routes. The admin socket stays private.

This signal proves the HTTPS edge responds, not application, database, backup or
host health. Semaphore's separate unauthenticated `/api/ping` GET expects 200.
A healthy edge with a failing application directs diagnosis toward that
application; both failing directs it first toward DNS, network, TLS and Caddy.
A dedicated hostname keeps the edge independent of application-route changes.

Before handing readiness to
[homelab-talos#423](https://github.com/supermorphic/homelab-talos/issues/423),
enroll private DNS and the protected health route without replacing unrelated
routes, authorize deployment and verify both endpoints from the intended
monitoring network using ordinary DNS and trusted hostname TLS. An IP URL,
`--insecure` or forced resolver override does not establish consumer readiness.
Do not stop a production backend to demonstrate independence. Homepage and Gatus
are consumers, never prerequisites for service or host recovery.

## External certificate contract

Caddy reads `fullchain.pem` and `privkey.pem` through the stable
`/etc/caddy/tls/<certificate_name>/current` interface. An atomic relative version
pointer selects a complete pair beneath that root. Files and directories must
have safe administrator ownership, Caddy read access and platform labels;
arbitrary path indirection is refused. Missing, unreadable, mismatched, expired
or hostname-incompatible material fails activation without exposing key contents.

Configuration, trust installation and certificate activation share a root-owned
exclusive deployment lock. Certificate operations keep it through selection,
forced reload and fresh served-certificate checks. Internal lock-held entry points
must verify the inherited exclusive lock; a flag alone grants no authority. TLS
takes its coordinator lock first and releases the proxy lock during ACME requests.

A pending TLS publication journal binds the selected generation, prior/candidate
boot configuration and committed desired revision. Ordinary configuration changes
cannot replace its recovery authority. Startup restores consistent disk state
without taking the TLS lock or requesting a certificate; runtime reconciliation
then validates the retained generation before any new issuance.

First publication activates only approved deferred routes. Failed first
publication restores them inactive and preserves unrelated routes. Prove route
absence in effective configuration as well as listener/certificate state;
unknown-SNI failure alone is insufficient on a shared listener. Certificate
renewal, retention and publication recovery remain owned by specification 007.

## Configuration activation and failure behavior

Use one fixed administrator-owned helper with bounded candidate paths and no
shell hooks. Candidates and committed configuration share a filesystem. Before
first route activation, establish a running admin-only configuration; when an
existing service is stopped, start its committed configuration first. Never wait
for systemd startup while holding the deployment lock.

1. Acquire the lock, recover any pending configuration transaction and repeat
   identity, path, certificate and listener preconditions. Require an active
   service after lock acquisition.
2. Validate as Caddy. If desired and effective state already agree, perform no
   reload. Otherwise record prior boot configuration and pending intent durably
   before replacing the boot file.
3. Reload through the protected socket and verify active configuration, declared
   listeners and certificates. Backend outage remains a separate condition.
4. Clear pending intent durably only after success. On failure, restore disk and
   runtime state and preserve both activation and restoration outcomes.

A failed load can retain the old configuration while leaving extra live sockets.
Check listener addresses and duplicate Caddy-owned sockets, allowing only bounded
transient overlap. If reload rollback cannot restore configuration, listeners and
served certificates, retain recovery state, release the lock and restart Caddy.
This exceptional recovery can interrupt all routes. Startup recovers transactions
under the lock before reading the boot file; afterward reacquire the lock and
verify the expected restoration has not been superseded. Failed first activation
returns to admin-only state. Ambiguous recovery requires an operator.

Order startup after network-online, but do not treat that target as proof that a
private DHCP address is assigned. Bounded restart handling permits transient bind
recovery and leaves persistent failures visibly failed. Explicit stops remain
stopped. Routine configuration changes reload a healthy process; binary or unit
changes may separately require restart.

## Operator recovery and acceptance

The canonical gateway owns target/action guards; the
[proxy playbooks](../../playbooks/reverse-proxy/) implement provision and
observational verification. Provision composes firewall state and verifies
activation without running full OS maintenance. Verify observes identity,
permissions, unit restrictions, admin access, effective configuration, listeners,
firewall and TLS without repair, reload or certificate requests.

Through authorized administrative access, inspect `systemctl status caddy` and
bounded `journalctl -u caddy` output, keeping deployment details private. Correct
a failed address, certificate or configuration condition before retrying. If
startup exhausted its rate limit, separately authorize
`systemctl reset-failed caddy.service` and `systemctl start caddy.service`, then
verify proxy and trusted client access. Do not delete transaction records to force
recovery. Package maintenance also needs allowed-client HTTPS and outside-boundary
denial checks; local observation does not prove DNS or network reachability.

Reconstruct from independently accessible Git and encrypted certificate/trust
and transaction state, after restoring administrative access and the OS baseline.
Caddy caches are not authoritative recovery material. Keep direct device bookmarks
and local management access independent of Pi-hole, Caddy, the host and Internet
connectivity. Failed trust or compatibility disables only the affected device
route until its checks pass.

Disposable fixtures cover effective permissions, protocol behavior, firewall
configuration, HTTP/WebSockets, independent health, certificate rotation,
listener leaks, interrupted transactions, failed initial activation and owned
cleanup. Fixtures must demonstrate their own effective isolation; extra nested
container capabilities never broaden production service authority. Container
firewall and unit observations do not prove host-kernel enforcement, enforcing
AppArmor, physical reboot, production trust or private client reachability.
Those and lost-host recovery require separate operator evidence.

## References

- [Podman foundation](006-podman-quadlet-foundation.md)
- [Command lifecycle](001-agentic-development-modernization.md)
- [Proxy activation source](../../roles/reverse_proxy/files/)
- [Caddy systemd deployment](https://caddyserver.com/docs/running)
- [Caddy configuration API](https://caddyserver.com/docs/api)
- [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
