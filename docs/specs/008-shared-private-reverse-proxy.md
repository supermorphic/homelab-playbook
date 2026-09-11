# Specification 008: Shared private reverse proxy

Issue: [#25 Shared private reverse proxy for NUC #4 services](https://github.com/supermorphic/homelab-playbook/issues/25)

Status: Approved implementation design, including the operator's subsequent
decision to use unpinned distribution packages and the existing OS maintenance
lifecycle. It is not evidence of live deployment.

## Purpose and scope

Provide one private HTTPS entry point for explicitly declared browser-based
services on an off-cluster host. Ansible owns configuration, systemd supervises
Caddy, and separately owned rootless Podman services expose local HTTP backends.
The initial target is NUC #4; implementation supports the repository's Debian 13
and Rocky Linux 9 baseline.

Issue #25 owns the proxy, static route schema, private ingress, configuration
activation, verification, and a disposable HTTP/WebSocket acceptance fixture.
Issue #5 owns ACME, Cloudflare credentials, certificate issuance, renewal,
deployment, and certificate recovery. Issues #4 and #7 own Semaphore and Forgejo
and their application-specific routes. This design does not deploy those apps.

Public Internet ingress, container discovery, Podman socket access, application
authentication, Kubernetes integration, and application backups are excluded.
Recovery must remain possible from an external workstation and trusted console.

## Deployment decision

Use a dedicated host-level Caddy service. The small static service set does not
need container discovery or a shared container network. The Podman foundation
deliberately provides neither privileged-port exceptions nor shared accounts.

| Concern | Host-level systemd service | Rootless Podman Quadlet |
| --- | --- | --- |
| TCP/443 | Service-scoped binding capability | Additional host port arrangement required |
| Identity | Dedicated non-login system account | Foundation account and subordinate allocations |
| External private keys | Direct read-only access | Mount, ownership mapping, and labeling contract |
| Separate application accounts | Reach host-loopback published ports directly | Configure container-to-host connectivity |
| Firewall | Existing baseline service extension | Same host policy plus container network behavior |
| Validate and reload | Direct commands under the service identity | Commands execute in the running container context |
| Logs and verification | Journald, systemd, and HTTPS probes | User manager, container state, and HTTPS probes |
| Recovery material | Binary, Git configuration, external certificate material | Also image availability and container setup |
| Operational simplicity | One host service and local backends | Additional runtime and network dependencies |

A rootful Quadlet could simplify port binding but adds a root-managed container
runtime boundary. Rootless host networking can simplify backend connectivity
but does not resolve privileged host-port binding. Neither offers a demonstrated
benefit for this service that outweighs the additional mechanisms.

## Service installation and ownership

Install the unpinned `caddy` distribution package without DNS-provider plugins.
Use Debian 13's native APT repository. Rocky Linux 9 uses its compatible EPEL
package, with the signed EPEL repository explicitly established by the role.
The OS maintenance trust check recognizes the standard `epel` and
`epel-cisco-openh264` repositories only when the exact root-owned Caddy ownership
marker is present, and requires their EPEL 9 signing key. This keeps repeat
provisioning and maintenance compatible with the installed Caddy package source.
The demonstrated need for EPEL is the Caddy package; no upstream Caddy repository
or standalone binary installer is introduced. Installation uses `state: present`;
existing OS maintenance owns package upgrades. Daily security updates retain
their existing distribution policy and depend on repository security metadata.
Do not use `caddy upgrade`, package holds, or version locks. Verification records
the installed version and validates required capabilities instead of requiring
one exact release. Package upgrades can restart the shared proxy and affect all
routes briefly; configuration rollback does not promise package-version rollback.

Use a dedicated `caddy` non-login system account and private group. It has no
sudo, SSH keys, Podman allocation, application-group membership, or access to
certificate-issuer credentials. Reject an incompatible existing identity or
unmanaged Caddy installation instead of silently taking it over. Numeric IDs
are not a cross-host persistence contract for this host service; certificate
deployment establishes ownership by account and group name after reconstruction.

Debian's package adds the account to `www-data` during installation and upgrades.
Reconcile supplementary groups during provisioning and in a root startup
precondition before Caddy executes. This keeps the dedicated identity contract
effective after package-triggered restarts without holding back package updates.

Run `caddy.service` as this identity. Limit its capability bounding and ambient
sets to `CAP_NET_BIND_SERVICE`. Apply `NoNewPrivileges`, a private temporary
directory, a read-only system filesystem, protected home directories, and a
restrictive umask. Grant writable state only where required for runtime and
Caddy state. Do not copy the upstream unit's broader capability set unchanged.
Retain enforcing SELinux on Rocky and the existing AppArmor baseline on Debian;
test required labels and access without disabling either mechanism.

| Path | Owner:group | Mode | Purpose |
| --- | --- | --- | --- |
| `/etc/caddy/` | `root:caddy` | `0750` | Administrator-owned configuration |
| `/etc/caddy/Caddyfile` | `root:caddy` | `0640` | Committed boot configuration |
| `/etc/caddy/tls/` | `root:caddy` | `0750` | External certificate deployment boundary |
| Certificate version directories | `root:caddy` | `0750` | Immutable certificate/key pairs |
| Certificate and private-key files | `root:caddy` | `0640` | Readable but not writable by Caddy |
| `/var/lib/caddy/` | `caddy:caddy` | `0700` | Private runtime state and caches |
| `/run/caddy/` | `caddy:caddy` | `0700` | Permission-protected admin socket |
| `/var/lib/homelab-reverse-proxy/` | `root:root` | `0700` | Configuration transaction recovery |

The role rejects unexpected symlinks at managed roots and writable parent
directories. Certificate selection uses only the explicitly defined version
symlink below; arbitrary path indirection is not accepted. Caddy cannot edit
its configuration, certificate files, deployment helper, or systemd unit.

## Declarative ingress and routes

Use an explicit `reverse_proxy` inventory group and these service inputs:

- `reverse_proxy_bind_addresses`: non-empty private host addresses, with no
  unspecified address or wildcard listener.
- `reverse_proxy_client_sources`: non-empty explicit private client CIDRs.
- `reverse_proxy_routes`: a complete list of route declarations, initially
  empty until the operator supplies certificates and approved services.
- `reverse_proxy_deferred_certificates`: empty or exactly `[infra]`; only this
  explicit dependency may be missing before first certificate publication.
- `reverse_proxy_trust_certificates`: named immutable route-specific device
  trust bundles supplied through protected inventory.
- Each local application route contains `hostname`, `backend_port`, and `certificate_name`.
  A hostname is an exact DNS name, a backend port is an integer from 1024 through
  65535, and a certificate name is a restricted filesystem component.

Reject duplicate hostnames, invalid types, wildcard hostnames, control
characters, arbitrary Caddyfile fragments, URL credentials, path traversal,
and unapproved upstream addresses. Local application routes construct upstreams as
`127.0.0.1:<backend_port>`. Each application owns its loopback port allocation
and Podman port publication; the proxy owns no application account or data.
Host loopback prevents remote access but is not a boundary between local users.

Issue #5 extends routes with an explicit private-device form: `hostname`,
`certificate_name`, and `backend`. The backend contains `transport` (`http` or
`https`), an RFC1918 or ULA `address`, and `port` from 1 through 65535. HTTPS also
requires `server_name` and `trust_name`, selecting
`/etc/caddy/trust/<trust_name>.pem`. Caddy verifies the backend certificate using
that route-specific trust and server name. HTTP accepts no TLS trust fields.
Local and device backend forms cannot be combined. Trust names identify immutable
content; use a new name when rotating trust so rollback can retain the old trust.
The shared renderer supports the distribution packages' TLS transport syntax.

The operator supplies live addresses, client networks, and sensitive deployment
names through the existing protected inventory process. Public examples use
synthetic hostnames and documentation addresses. Offline tests inject synthetic
inputs directly and never decrypt inventory. The HTTPS source list is separate
from SSH policy; an operator may explicitly set it to the management-source list.
This design does not assume that SSH clients and HTTPS clients are identical.

Render exact hostname routes on TCP/443 with explicit certificate files. Disable
automatic certificate management and HTTP redirects with `auto_https off`.
Enable HTTP/1.1 and HTTP/2 only. Do not create TCP/80 or UDP/443 listeners.
Unknown hostnames receive no application route. No forward-proxy or arbitrary
upstream interface exists. Preserve normal Caddy HTTP and WebSocket forwarding;
do not trust client-supplied forwarded headers as a separate upstream proxy.

The Debian 13 package is based on Caddy 2.6.2, which does not implement
`stream_close_delay`. Use native WebSocket behavior: successful configuration or
certificate refreshes close established WebSockets and clients reconnect. Failed
configuration validation must preserve existing connections. A removed route
rejects new connections after reload. Do not add a newer package source merely
to delay WebSocket closure.

An empty route list renders an admin-only configuration with no HTTPS listener
and no required certificate pair. Removing the final route removes ingress
allowances as well as listeners. It does not delete externally owned TLS files.

Ansible supplies a root-only candidate desired manifest and ingress metadata
after firewall verification. The fixed `apply-desired` helper validates their
binding, renders effective routes, and commits configuration plus the desired
manifest in one recoverable transaction. The committed `desired.json` contains
the exact original manifest string and ingress metadata with its SHA256 revision.
Only routes explicitly depending on the absent first `infra` certificate are
deferred. Missing or malformed certificates for other routes fail activation.
Private ingress policy follows declared routes so TLS can later activate them
without becoming a second firewall controller. An empty effective route list
still has no network listener. Removing all desired routes removes allowances.

## Firewall integration

The existing security baseline remains the sole firewall policy owner. Compose
the proxy's source-scoped TCP/443 allowance alongside
`security_baseline_firewall_services`, preserving other explicitly declared
service extensions. Both OS and proxy operations derive the same complete
desired policy so a later OS provision does not remove a valid proxy allowance.

Use explicit TCP/443 rich rules, as the baseline already does for TCP/22, to
avoid depending on a platform's HTTP/3 service definition. Read back runtime
and permanent source rules. Reuse the existing
guarded firewall reconciliation and active-SSH-peer checks; do not introduce
ad hoc rules or a second independent firewall controller.

Before exposing a newly configured listener, prove that the desired private
policy is effective. Missing required firewall state stops provisioning.
IPv4 and IPv6 listeners and sources receive equivalent restrictions. No route
opens its backend port through firewalld. Production reachability evidence
requires an allowed client and a client outside the allowed boundary.

## External certificate contract

For each `certificate_name`, issue #5 supplies immutable version directories
below `/etc/caddy/tls/<certificate_name>/`. The `current` symlink selects one
version containing `fullchain.pem` and `privkey.pem`. Caddy references these
stable paths; switching the directory pointer selects the pair together.
Targets must remain below that certificate's root and have the required owner,
group, permissions, and platform labels. The `current` symlink itself must also
be owned by `root:caddy`.

Issue #25 creates the shared directory and validates the consumption interface.
It neither generates production certificates nor copies their contents through
the controller. Missing, unreadable, mismatched, expired, or hostname-incompatible
material fails activation. Diagnostics report the failed condition without
printing the private key or protected configuration. Issuer account credentials
never enter Caddy's filesystem or environment.

Configuration and certificate activation share a root-owned host lock. Issue #5
must hold that lock across version selection, validation, certificate
replacement, and post-deployment TLS verification. Its deployment procedure
retains the previous version and restores the pointer and running certificate
after failure. The proxy's certificate-refresh entry point supports use inside
this transaction without taking the same lock twice. Direct operator reloads
acquire the lock themselves.

The helper is `/usr/local/libexec/homelab-reverse-proxy`; the shared lock is
`/run/lock/homelab-reverse-proxy.lock`, owned by root with mode `0600`.
`reload` acquires this lock. The certificate automation integration forms
`reload --lock-held` and `verify --lock-held` require the existing exclusive lock
inherited on file descriptor 9. The helper validates that inherited lock;
the flag alone does not grant execution authority. Certificate automation must
retain it through version selection, reload, and served-certificate verification.

Issue #5 supplies the fixed `homelab-tls-caddy` adapter. TLS takes its coordinator
lock before the Caddy lock and releases the Caddy lock during ACME network work.
Its root-only publication journal binds certificate selection, prior and candidate
boot configurations, and the committed desired revision. While that transaction
is pending, ordinary configuration changes cannot replace its recovery authority.
Startup recovery restores consistent certificate and configuration disk state
without acquiring the TLS coordinator lock or requesting a certificate. Runtime
recovery then verifies or retries the retained generation before new issuance.

First publication activates only declared routes dependent on `infra`. Failed
first publication restores their previous inactive state while preserving
unrelated routes. Verify route absence in the active configuration and check
remaining listeners and certificates; an unknown-SNI handshake alone does not
establish route absence on a shared listener.

Device trust installation uses the same deployment lock. The fixed
`install-trust` action reads `/var/lib/homelab-reverse-proxy/trust.candidate.json`,
validates its bounded PEM bundles, and publishes each new name without replacing
an existing file. Existing names must have the exact declared contents and safe
metadata. Concurrent declarations cannot change another route's trust bundle.

The `reload` entry point validates the committed configuration as the service
account and forces Caddy to reload it from the selected `current` certificate
paths. It fails if the service is inactive before the transaction. Issue #5 owns
retries, renewal timing, expiry monitoring, version retention, and recovery of
interrupted certificate deployments. Git plus separately recoverable certificate
material is sufficient to reconstruct the proxy; Caddy cache files are not
authoritative recovery data.

## Configuration activation and failure behavior

Provide one root-owned activation helper used by Ansible. It accepts only the
managed candidate path and fixed service paths, not arbitrary shell commands.
Stage candidates on the same filesystem as the managed configuration and use
atomic replacement with durable transaction metadata.

On first provisioning, establish and start a committed admin-only configuration
before beginning route activation. It has no network ingress or certificate
dependency. If an existing service is stopped, start its committed configuration
before beginning the replacement transaction. Never wait for systemd startup
while holding the deployment lock. After obtaining the lock, require the service
to be active; a concurrent stop fails activation without installing a candidate.

1. Acquire the host deployment lock and recover any interrupted configuration
   transaction before accepting another candidate.
2. Repeat identity, filesystem, certificate metadata, and listener preconditions.
   Validate the candidate using `caddy validate` under the Caddy identity.
3. If the desired configuration equals the committed configuration and observed
   service state matches, report no change and perform no reload.
4. Record the previous boot configuration and a pending transaction marker
   durably before replacing the boot configuration.
5. Reload the running service through its Unix socket. Verify active
   configuration and listener state. A backend
   outage is reported separately and does not make unrelated routes disappear.
6. On success, durably clear the pending marker and report a change. On failure,
   restore the previous boot configuration, restore runtime state if needed,
   and report both activation and recovery outcomes.

A failed configuration load can leave a partially started HTTP listener behind
in the distribution Caddy releases. The previous configuration can still be
active while this extra listener continues serving traffic. The helper checks
both listener addresses and duplicate sockets owned by the Caddy process. It
rechecks brief listener overlap before treating persistent duplicates as failure.
It restores the committed disk configuration and attempts runtime rollback through
reload, including a check of the served certificates. If runtime rollback cannot
restore the declared listeners, active configuration, and served certificates, it retains durable recovery state, releases the deployment lock,
and restarts Caddy. This recovery can briefly interrupt all routes.

A startup precondition acquires the deployment lock and recovers a pending
transaction before Caddy reads its boot file. After restart, activation reacquires
the lock and verifies the previous boot configuration, runtime, listeners, and
served certificates. It reports recovery as incomplete if another activation
changed the boot configuration in the meantime. Activation never waits for
systemd startup while holding the lock.

Failed first route activation restores the admin-only configuration, with no
HTTPS listener or candidate boot configuration. An ambiguous or unrecoverable
transaction stops with an operator recovery requirement. Configuration changes
do not restart a healthy process. Binary and systemd-unit upgrades may require
a controlled restart and must be reported separately from routine reloads.

## Operator interface and implementation boundaries

Add `reverse-proxy provision` and `reverse-proxy verify` through the existing
`mise run playbook -- <playbook> <action> <inventory> [ansible-args...]` gateway.
Apply the existing credential and task-selection guards to these host actions.
Provision one host at a time after the OS baseline; include verification.

Use a dedicated role with focused input validation, installation, filesystem,
configuration activation, and verification units. Reuse the baseline firewall
entry points and common desired-policy composition. Do not run full OS package
maintenance as a side effect of proxy provisioning. Keep service code separate
from the generic Podman foundation.

Standalone verification observes identity, installed version, file metadata,
systemd restrictions, admin-socket permissions, active configuration, listeners,
firewall state, and served TLS identities for configured routes. It does not
reload, repair state, pull images, issue certificates, or create test routes.
Service logs use journald with the baseline retention policy. HTTP access
logging is disabled by default; do not add request credentials, query strings,
or protected configuration content to deployment diagnostics.
Document `systemctl status` and `journalctl -u caddy` as normal diagnostics.

Register the temporary backend experiment as a `test` workflow. It creates
only run-owned synthetic routes, backend processes, and disposable certificate
material, reports primary and cleanup failures separately, and verifies removal.
It is never embedded in observational verification.

## Validation and acceptance

Extend registered offline validation and its classifier to cover the subsystem.
Use independent effective-state and protocol assertions rather than checking
only generated text. Disposable Debian 13 and Rocky Linux 9 evidence covers:

- Input rejection, service identity, filesystem isolation, and effective unit
  restrictions, including denial of cross-account certificate and admin access.
- Exact TCP/443 binding, absent HTTP and HTTP/3 listeners, and the composed
  firewall policy without backend port openings.
- HTTPS trust using a disposable CA, SNI and hostname verification, two distinct
  hostname routes, ordinary HTTP, and bidirectional WebSocket messages.
- Invalid syntax, unreadable and mismatched keys, expired certificates, and
  hostname mismatch, while a previously healthy route continues serving.
- A reload-time failure after opening one listener that passes static validation,
  disk and runtime rollback with exactly one remaining listener, interrupted
  transaction recovery, and failed initial activation.
- Certificate replacement serving a new external certificate, reconnecting
  WebSockets, and unchanged configuration producing no Ansible change.
- Test route removal, eventual stream closure, listener removal for an empty
  route list, and complete run-owned fixture cleanup even after test failure.

Run `mise run bootstrap` after dependency changes, focused `validate:fast` and
`validate:ansible` during development, and `mise run ci:changed` before completion.
Register any required container scenario in local and GitHub CI dispatch together.
Do not weaken host restrictions to accommodate nested container limitations.

The rootless proxy test containers add `SYS_PTRACE` so their administrator can
observe Caddy-owned sockets with `ss -p`. They also add `SYS_ADMIN` so systemd
can create the nested coordinator filesystem sandbox. Without it, Debian's
service can fail before execution, and systemd can skip filesystem restrictions
in containers. An early disposable service verifies that `/usr/local` is
read-only while the declared `/var/lib` write exception remains writable.
The disposable services inherit the system manager's root identity rather than
setting `User=root`: explicit user setup in nested Debian systemd prevents the
adapter from switching to the Caddy user. The early probe verifies effective
root identity and a successful unprivileged user switch, and the integrated
driver independently checks its root identity. The remaining sandbox settings
stay in place; production units retain their explicit service identities.
These capabilities belong to the rootless test container; the
managed Caddy service still receives only `CAP_NET_BIND_SERVICE`, and the
production coordinator restrictions remain unchanged.
Container checks establish permanent firewall configuration, not host-kernel
firewall enforcement or enforcing SELinux behavior.

Offline results do not prove private-network reachability, physical boot,
production certificate trust, certificate renewal, or recovery from a lost host.
Those require separately authorized operator evidence. Before live execution,
reconfirm the exact playbook, action, inventory, host limit, and extra arguments.
Production acceptance waits for approved private inventory inputs and external
certificate material. Implementation does not create credentials to bypass that
dependency.

## References

- [Specification 006](006-podman-quadlet-foundation.md) defines separate rootless
  service accounts and external recovery requirements.
- [Specification 003](003-os-maintenance-security-baseline.md) defines firewall
  ownership, mandatory access control, and host validation boundaries.
- [Caddy systemd deployment](https://caddyserver.com/docs/running) documents
  service ownership, logging, reload, and SELinux installation considerations.
- [Caddy command line](https://caddyserver.com/docs/command-line) documents
  configuration validation.
- [Caddy configuration API](https://caddyserver.com/docs/api) documents failed-load
  rollback and permission-protected Unix administration sockets.
- [Caddy TLS configuration](https://caddyserver.com/docs/caddyfile/directives/tls)
  documents externally supplied certificate and key files.
- [Caddy global options](https://caddyserver.com/docs/caddyfile/options) documents
  disabling automatic certificate management and selecting HTTP protocols.
- [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
  documents HTTP forwarding, WebSockets, and streaming behavior during reload.
- [Podman networking](https://docs.podman.io/en/stable/markdown/podman-run.1.html)
  documents host networking and rootless container-to-host connectivity.
