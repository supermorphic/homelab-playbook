# Specification 007: Off-cluster TLS trust

Issue: [#5](https://github.com/supermorphic/homelab-playbook/issues/5)

## Decision and ownership

Use production ACME with Cloudflare DNS-01 and separate certificate owners:

| Consumer | Certificate owner | Browser connection |
| --- | --- | --- |
| UDM Network | Native UniFi OS issuer | Direct to UDM |
| Separate Protect endpoint | Its hosting console, after capability verification | Direct to Protect endpoint |
| UNAS Drive | Native UniFi OS issuer | Direct to UNAS |
| Off-cluster host applications | Dedicated host issuer | Through shared Caddy |
| ARRIS S34 and Room Alert 3E | Host issuer supplies Caddy's certificate | Caddy forwards to the private device |

This hybrid topology avoids distributing one wildcard private key to every
appliance. Protect remains a separate endpoint; do not infer its hosting console
or native certificate capability from Network, UDM, or UNAS versions.
[Specification 008](008-shared-private-reverse-proxy.md) owns Caddy deployment,
private ingress and routing. This spec owns issuance, certificate publication,
served-certificate verification and device TLS integration.

Use one exact hostname per UniFi console, outside the dedicated Caddy namespace.
Synthetic examples are `udm.example.com`, `protect.example.com` and
`nas.example.com`. Local DNS points directly to each console. Caddy's separately
issued certificate contains exactly the DNS SAN `*.infra.example.com`; it covers
neither the namespace apex, deeper names, direct UniFi names nor cluster names.
Expanding that SAN set requires explicit review. Proxied hostnames resolve to
Caddy's private listener, which connects to the actual device management address.

Public DNS serves challenge records; private endpoints need no public A/AAAA
record, Cloudflare proxying, WAN port forward or inbound ACME listener. Local
DNS overrides must preserve public challenge resolution. Stable hostnames survive
private address changes without reissuance. Certificate Transparency publishes
certificate names. Cluster keys, ACME accounts and DNS credentials remain separate.

## Credentials and issuance

Each console and the host issuer receive separate Cloudflare tokens. Use DNS Edit
and Zone Read restricted to the required zone, never a global API key or all-zone
scope. This proposed permission set still requires successful native UniFi
issuance; it is not a vendor minimum guarantee. Zone scope permits edits across
that zone: separate tokens permit independent rotation, not record isolation.
Keep token recovery outside the managed host and services.

The host uses a pinned, checksum-verified lego artifact under a dedicated
non-login issuer account, managed by the
[TLS role](../../roles/tls_automation/tasks/main.yml). A host process avoids an
extra container and cross-account publication mechanism for a short-lived client
with no listener. Caddy owns neither DNS credentials nor ACME account keys.
The credential source is root-owned and delivered only to the issuer through
private systemd credentials. Routine renewal needs no live age identity on the host.

Serialize issuance and deployment. Retain ACME account state, respect the pinned
client's renewal eligibility and ARI behavior, and bound requests and execution.
Use public recursive and authoritative DNS checks independently of private
application DNS. An ordinary provision must not force a new ACME order.

The administrator-owned coordinator invokes a fixed issuer unit and a fixed
Caddy adapter, with sanitized environments and argument vectors. The issuer can
write only private ACME state and certificate data; it cannot select privileged
paths, hooks, commands, configuration or verification targets, or invoke publication.
The coordinator's identity switch retains only the capability required by the
systemd sandbox and clears capabilities when executing as Caddy.

## Certificate handoff and recovery guarantees

Separate issuer-private state, root-owned immutable snapshots and published
generations readable by Caddy. Snapshot bounded regular files without following
issuer-controlled links. Validate key/leaf agreement, exact SAN equality, server
purpose, current validity, usable remaining lifetime and public chain trust.
Reject additional SANs, other SAN types and production publication of staging
certificates. Use independent cryptographic checks, not issuer metadata.

Publish a complete certificate/key generation by atomically switching the
relative `current` symlink under `/etc/caddy/tls/infra`. Keep that parent stable.
Root-only preparation and transaction state stay on the same filesystem under
`/etc/caddy/tls/.homelab-tls-private`; Caddy cannot traverse them. Caddy reads
published keys but cannot modify them. Resolve its package-owned group by name
after reconstruction rather than treating its numeric GID as a portable allocation.

Take the TLS coordinator lock before the shared proxy deployment lock; release
the proxy lock during ACME network requests. Bind publication to the proxy's
committed desired routes and verified ingress. Repeat identity, path and active
state checks immediately before switching. Validate the candidate as Caddy,
force reopening of certificate files and check a fresh TLS handshake for every
declared hostname/listener: trust, hostname, validity and exact leaf fingerprint.
A reload exit code or valid disk file is insufficient. Changed chain material
requires publication even if the leaf fingerprint is unchanged.

Retain the prior generation until activation succeeds. An expired prior generation
must not block a valid replacement. Failed activation restores and verifies the
prior generation; failed first activation proves affected routes inactive while
preserving unrelated routes. Record issuance, activation and restoration outcomes
separately. Backend outage is a separate condition and does not roll back a correct
certificate.

Durable transaction state survives interruption and retries retained candidates
without a new order. Repeat current validation and served-certificate checks on
recovery. If neither generation is usable, retain the journal and block issuance;
never delete a guard or journal to bypass failed recovery. Complete recovery
durably before cleanup. Observational verification does not alter attempt status;
an interrupted `in_progress` record is not success evidence.

## Minimum native UniFi sequence

This remains an operator procedure, with no supported repository automation for
console certificate enrollment. Each credential or certificate change requires
separate authorization; this design grants none.

1. Retain direct console access and independently recoverable Cloudflare access.
   Confirm the hosting console offers native Let's Encrypt with Cloudflare,
   including the separately hosted Protect endpoint. Establish correct time and
   outbound DNS/HTTPS. If capability is absent, preserve existing access and
   resolve that console's method separately.
2. Choose its exact hostname and add the direct hostname/address record to every
   local DNS server used by management and recovery clients. Confirm resolution
   from such a client; direct IP URLs will not match the hostname certificate.
3. In Cloudflare **My Profile > API Tokens > Create Token**, create a custom
   token with the zone-restricted permissions above, one per console. Save its
   once-displayed value in an independently recoverable password manager. Review
   expiry and egress-IP restrictions against WAN changes.
4. Sign in directly to that console. Open **Settings > Control Plane > Console >
   Certificates**, then **Add New Certificate > Let's Encrypt**; labels can vary.
   Enter the chosen domain, leave **Manual DNS Setup** unchecked, select
   **Cloudflare** and enter that console's token privately. Select **Add**, wait
   for issuance and activate the certificate if the list offers a separate action.
   Do not submit repeated requests while one is pending.
5. Visit the hostname using local DNS and inspect the certificate actually served:
   hostname coverage, public trust and validity. Test login/MFA and Network
   navigation; on Protect test live view and playback; on UNAS test Drive
   navigation and a disposable upload/download. Retain direct access until these
   pass. Browser TLS does not configure SMB, NFS or every native client.
6. Leave the automatic provider configuration available. Record fingerprint or
   serial and expiry privately; later observe a replacement served certificate
   with later validity to prove automatic renewal. Initial issuance alone does
   not prove renewal. Check status after OS updates and periodically; notification
   configuration is a separate concern.

For failure, check zone permissions, expiry, public challenge resolution/delegation,
CAA, time and outbound access without broadening credential scope. For rotation,
create a replacement scoped token, update the installed provider settings and
verify issuance before revoking the old token. Inspect the installed UI before
removing a working certificate; editing support varies by console version.

Keep console access, hostnames, DNS recovery and token records independent of
Caddy, local DNS availability, the managed host, Kubernetes, Forgejo and Semaphore.
Do not assume a console backup restores keys or provider credentials without a
successful restore. After replacement, restore direct access and DNS, enroll the
native issuer if needed and repeat served-certificate and application checks.

## Host bootstrap and diagnosis

The canonical playbook gateway owns command syntax and target guards; the
[TLS playbooks](../../playbooks/tls/) own implementation. Capability installation
with the renewal timer disabled must make no ACME request. First issuance cannot
be bootstrapped by enabling the timer:

1. Enroll protected routes and private ingress; provision Caddy with the first
   `infra` certificate explicitly deferred. Unrelated routes remain available.
2. Provision TLS with `tls_automation_timer_enabled: false`; install the real
   adapter before issuance. Enroll its separate scoped token through SOPS and
   reconcile credentials.
3. Separately authorize `mise run playbook -- tls renew production --limit <host>`.
   Then run the matching `tls verify` and inspect trusted client access.
4. Only after a clean active generation and successful endpoint verification,
   authorize ongoing renewal, enable the timer and provision again. Timer
   enablement fails closed when any credential, adapter or recovery check fails.

The current TLS gateway accepts only production. Any staging experiment requires
separate reviewed state, account and output roots; staging material cannot reach
production publication or listeners.

For issuance failure, inspect the bounded private issuer diagnostic at
`/var/lib/homelab-tls-issuer/last-issue.log` locally through authorized access.
Compare its timestamp with the attempt: failure before invocation can leave it
stale. Raw client output is sensitive and never belongs in Ansible, journald,
issues or CI artifacts. Publication failures use the coordinator's safe journal
output. Correct the cause and use the normal authorized reconciliation; do not
invoke lego directly or relax isolation for diagnosis.

Keep independent encrypted backups of issuer ACME state, `/etc/caddy` certificate,
trust and transaction state, and the TLS/proxy coordinator state. Rebuild from
GitHub and workstation-held credentials: restore administrative access and the OS
baseline first, reconcile with renewal disabled, restore available state and
reader ownership, verify endpoints, then enable renewal. If reissuance is needed,
first restore DNS, time, outbound connectivity and credential access, considering
CA rate limits. Nonempty legacy publication state or an old pending journal
requires operator recovery rather than silent migration.

## Private device integration and acceptance

Establish the ARRIS HTTPS certificate identity through an independent trusted
observation before installing a route-specific trust anchor. Retain name, chain
and validity verification. Captured self-signed material may lack usable SANs or
change after firmware updates. Failed trust keeps only that route inactive;
there is no disabled-verification or HTTP fallback. Keep direct modem access.

Room Alert 3E uses its explicitly confirmed HTTP management port. Browser traffic
is HTTPS to Caddy, while login data and pages are plaintext on the private device
hop. Restrict that path to trusted management networks; it is not end-to-end
TLS. The proxy stores no device login credentials. Add redirect/cookie exceptions
only after observed compatibility failures. Test login/logout, navigation,
redirects, backend transport and unrelated routes without rebooting or resetting
a device. Direct recovery must work without Caddy or Pi-hole.

Offline fixtures prove cryptographic rejection, privilege boundaries, atomic
publication, forced reload, rollback and interrupted recovery using synthetic
names and disposable keys. Live issuance, a later UniFi renewal, separate Protect
capability, host timer/recovery, device workflows and physical reconstruction
require separate operator evidence. Technitium's host and protocols remain an
unresolved consumer input; do not invent a target to close issue #5.

## References

- [SOPS credential and recovery boundary](005-sops-age-secrets.md)
- [Command lifecycle](001-agentic-development-modernization.md)
- [TLS coordinator source](../../roles/tls_automation/files/tls_runtime/)
- [UniFi certificate settings](https://help.ui.com/hc/en-us/articles/34210126298775-Self-Hosting-UniFi)
- [Cloudflare API tokens](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)
- [lego Cloudflare provider](https://go-acme.github.io/lego/dns/cloudflare/)
- [Caddy reload behavior](https://caddyserver.com/docs/command-line#caddy-reload)
- [ARRIS Web Manager](https://arris.my.salesforce-sites.com/consumers/articles/knowledge/S33-Web-Manager-Access)
- [Room Alert port requirements](https://avtech.com/articles/14315/list-of-ports-required-by-room-alert-products-2/)
