# Off-cluster TLS playbooks

These playbooks install and operate the NUC host issuer from
[specification 007](../../docs/specs/007-off-cluster-tls-trust.md). They target
only the `tls_issuer` inventory group through `mise run playbook`. Production
registers `nuc4` in both `tls_issuer` and `reverse_proxy`, with renewal
disabled. Inventory membership does not authorize a live playbook run.

The TLS command family accepts only the `production` inventory. The gateway
rejects staging and frozen inventory selections because this implementation has
one fixed production state and ACME policy. A separate staging workflow needs
its own reviewed state roots, inventory registration, and CA policy.

- `provision.yml` validates the host and declared allocation before mutation,
  installs the fixed issuer and coordinator, and reconciles the timer state.
- `renew.yml` starts the fixed root `homelab-tls-renew.service`. The unit runs
  the no-argument coordinator with a one-hour outer timeout. The blocking start
  also waits for a reconciliation already started by the timer and returns its
  final result.
- `verify.yml` observes the installed boundaries, active publication, and every
  declared TLS endpoint. It does not request or publish a certificate.

All actions require key-only SSH as the `ansible` account and passwordless
sudo. The gateway rejects password inputs and task-selection options that can
skip preflight checks.

## Required inputs

Public inventory declares stable issuer IDs and a disabled timer. Supply live
namespace, contact, routes, listener addresses, and client networks in
`inventory/production/host_vars/nuc4/secrets.sops.yml` through the existing
[operator SOPS workflow](../../docs/guides/sops-secrets.md). Host variables
override the empty group defaults without duplicating values between TLS and
Caddy. This synthetic example shows the required shape:

```yaml
tls_automation_namespace: infra.example.com
tls_automation_email: acme@example.com
reverse_proxy_bind_addresses:
  - 10.20.30.40
reverse_proxy_client_sources:
  - 10.20.0.0/16
reverse_proxy_routes:
  - hostname: room-alert.infra.example.com
    certificate_name: infra
    backend:
      transport: http
      address: 10.20.30.60
      port: 80
tls_automation_issuer_uid: 2010
tls_automation_issuer_gid: 2010
tls_automation_timer_enabled: false
```

The examples are synthetic. `tls_automation_namespace` must be one `infra.*`
namespace beneath the registered domain. The role derives the sole certificate
SAN as `*.<namespace>`. Each endpoint hostname must be directly beneath that
namespace and each address must be a private unicast IP literal.

TLS derives verification targets from every `infra` route and every declared
Caddy listener at TCP/443. Do not maintain a separate endpoint list. Each route
must fit the exact wildcard namespace. The public `tls_issuer` defaults declare
`reverse_proxy_deferred_certificates: [infra]`, allowing those routes to remain
inactive until the first certificate exists.
Provisioning rejects a declaration that differs from Caddy's committed manifest
or its verified ingress binding. Renewal repeats this check under the deployment
lock before certificate operations.

Provision Caddy first. TLS resolves the existing `caddy` group by name; its
numeric GID is not an operator allocation. A supplied legacy
`tls_automation_reader_gid` must match that group. Issuer IDs must differ from
the reader GID. The declared `2010:2010` issuer allocation is checked for
collisions before mutation. The role records the allocation in
`/etc/homelab-tls/.issuer-account.json`. It rejects identity collisions,
partial account state, allocation changes, and an existing account without the
matching root-owned record.

Supply `tls_automation_cloudflare_token` only through operator-managed SOPS
inventory. It is written to `/etc/homelab-tls/cloudflare-token` as `root:root`
mode `0600` and is exposed to the issuer only through systemd
`LoadCredential`. If the variable is omitted while the timer is disabled, the
role preserves any existing credential file and can install a credential-free
capability. The host does not need an age identity for routine renewal.

## Disabled installation and bootstrap

The issuer uses Cloudflare's public recursive resolver at `1.1.1.1:53` for
ACME DNS discovery and propagation checks. It checks both recursive and
authoritative DNS responses. Permit outbound DNS access to that resolver and
the zone's authoritative nameservers. The host's system resolver and private
application DNS remain unchanged; local DNS overrides and caches do not control
the issuer's challenge checks. Public resolver caching can still delay a check;
inspect the private diagnostic before retrying a failed issuance.

The timer defaults to disabled. A disabled TLS provisioning run requires the
installed Caddy capability and declared route inputs, but no Cloudflare
credential. It installs pinned lego 5.4.1, the Python runtime, the real fixed
Caddy adapter, state boundaries, and systemd units. It does not contact an ACME
server or publish a certificate.

Issue #5 installs `/usr/local/libexec/homelab-tls-caddy`. The adapter has
three fixed actions: `validate <root-owned-candidate-directory>`, `reload`, and
`deactivate`. It must validate Caddy's real candidate access and configuration,
force the service to reopen certificate files, and prove the route inactive
after a failed first publication. It integrates with the host `caddy.service`
and package-owned `caddy:caddy` identity. The root coordinator can write only
its managed state and transaction roots: `/var/lib/homelab-tls`, `/etc/caddy`,
and `/var/lib/homelab-reverse-proxy`. The issuer still writes only its private
ACME state and receives the Cloudflare credential through systemd.

The root coordinator explicitly retains `CAP_SETUID` through
`AmbientCapabilities` so it can run Caddy validation and reload commands as
the `caddy` account. Systemd 257 can otherwise drop that capability during
seccomp setup when `User=root` and `NoNewPrivileges=yes` are combined.
The switch to Caddy's UID clears ambient capabilities; the Caddy commands
run without permitted or effective capabilities. Existing sandbox restrictions
remain enabled.

Use this sequence for the first certificate:

1. Declare the intended routes and private ingress in protected inventory.
   Run an authorized `reverse-proxy provision production --limit nuc4` through
   `mise run playbook --`. Provisioning verifies the firewall and commits the
   desired manifest. Routes using the pending `infra` certificate stay inactive;
   unrelated routes retain their certificates and service.
2. Run authorized `tls provision production --limit nuc4` with the timer false.
   The credential can be omitted for capability installation. Add the scoped
   Cloudflare token through SOPS and provision again before initial renewal.
3. Explicitly authorize and run one renewal:

   ```bash
   mise run playbook -- tls renew production --limit <host>
   ```

4. Verify the active certificate and all declared endpoints:

   ```bash
   mise run playbook -- tls verify production --limit <host>
   ```

5. After bootstrap succeeds, explicitly authorize ongoing renewal, set
   `tls_automation_timer_enabled: true`, and run provisioning again. Provision
   performs the same full observational verification before it enables and
   starts the twice-daily timer.

Timer enablement fails closed if the credential, fixed adapter, active
generation, clean transaction state, unit metadata, or endpoint verification
is unavailable. Do not enable the timer to bootstrap the first certificate.
The issuer service has a 15-minute request timeout. The root reconciliation
service has a one-hour timeout so serialized recovery, issuance, activation,
endpoint checks, and rollback remain inside one finite unit boundary.

## Recovery and evidence boundary

If a replacement and its rollback both fail, the journal retains the replacement
for a later bounded retry. Recovery repeats certificate and adapter checks and
fresh TLS verification. A successful retry does not start issuance and preserves
both prior failure outcomes in the current status. Invalid retained material
keeps recovery blocked; do not remove the journal to bypass it.

`status.json` under the private state directory records the current attempt. An
`in_progress` attempt can indicate an interrupted operation; it is not evidence
of successful completion. Observational verification does not change status.

If issuance fails, inspect the issuer's private diagnostic on the target host:

```bash
sudo tail -n 80 /var/lib/homelab-tls-issuer/last-issue.log
```

Each lego invocation replaces this file and retains only the final 64 KiB of
combined standard output and standard error. Output is updated while lego runs,
including before a timeout. The file is owned by `svc-acme`, mode `0600`, inside
its `0700` state directory; only that account and root can read it. Ansible,
journald, and the coordinator's status receive no raw lego output. Treat the
file as sensitive: inspect it locally and redact credentials and infrastructure
identifiers before sharing an error. Do not attach it to issues or CI artifacts.

Check the file's modification time against the failed attempt. A failure before
lego starts may leave an older diagnostic or an empty file. Reprovisioning
installs updated runtime code; it does not rerun issuance. After correcting the
reported cause, a separately authorized `tls renew` retries through the normal
coordinator and its recovery checks. Do not invoke lego directly or change the
service's credential and isolation settings to obtain diagnostics.

If issuance succeeds but publication fails, inspect the coordinator journal:

```bash
sudo journalctl -u homelab-tls-renew.service --since "5 minutes ago" --no-pager -n 30
```

The Caddy adapter reports its action and safe validation errors there. Raw
Caddy output and unclassified exception contents remain suppressed. A successful
issuance alone does not prove that Caddy has published the certificate.

Keep encrypted backups of `/var/lib/homelab-tls-issuer` ACME account state,
`/etc/caddy` configuration, trust, certificate and recovery state,
`/var/lib/homelab-tls` status,
and `/var/lib/homelab-reverse-proxy` configuration recovery state. Preserve
issuer ownership and private key confidentiality. On a replacement host, restore administrative access and
the OS baseline first. Reconcile the role with the timer disabled, restore
state when available, reconcile certificate-reader ownership to the installed
`caddy` group, run one authorized renewal
if necessary, verify the active endpoints, and only then enable the timer.

If ACME account state is unavailable, restore independent DNS, time, outbound
network, and SOPS credential access before an explicitly authorized renewal.
Account for CA rate limits. Direct appliance access remains the recovery path
when Caddy or local DNS is unavailable.

Certificate preparation is part of production publication, not an ACME staging
environment. Root-only temporary files and the publication journal live under
`/etc/caddy/tls/.homelab-tls-private`; complete readable generations live under
`/etc/caddy/tls/infra`. Keeping them on one filesystem permits atomic moves.
The previous `/var/lib/homelab-tls/published` layout is not migrated silently:
nonempty state or an old pending journal blocks provisioning for operator recovery.

The registered `system_maintenance/baseline` Molecule scenario installs real
admin-only Caddy and disabled TLS on disposable Debian 13 hosts.
It checks identity isolation, file metadata, parsed units, missing-credential
rejection, manual-start waiting, and the runtime with distribution Python and
cryptography. The `reverse_proxy/default` scenario covers integrated certificate
activation and recovery. These tests make no ACME requests and do not establish
production issuance, device browser compatibility, or physical boot evidence.
