# UniFi certificates with Cloudflare DNS

Use UniFi OS certificate management to issue and renew a separate publicly
trusted certificate on each UniFi console. This is the operator-managed UniFi
part of the hybrid TLS design selected for issue #5. The NUC #4 host issuer and
publication capability are implemented with automatic renewal disabled by default;
see the [TLS playbook README](../../playbooks/tls/README.md). Its Caddy adapter
and live deployment remain separate integration work.

UniFi consoles retain their own certificates and Cloudflare tokens. They do not
receive the Caddy wildcard private key or depend on NUC #4 for renewal. Caddy is
the intended TLS endpoint for NUC #4 applications and proxied private device
interfaces. The [hybrid TLS design](../specs/007-off-cluster-tls-trust.md) records
the ownership boundaries. The [reverse-proxy guide](reverse-proxy.md) defines DNS,
backend transport, acceptance, and recovery requirements. Each device route
still requires separate compatibility verification.

This guide adds no Ansible deployment or automatic console configuration.
The operator performs the steps below in Cloudflare, the local DNS service,
and UniFi. Never put tokens, private keys, or screenshots containing credentials
in repository files, issues, pull requests, or CI output.

## Prerequisites and scope

- A UDM SE and UNAS Pro with the native Let's Encrypt certificate form and
  Cloudflare provider available. The setup was reviewed with operator-reported
  UniFi OS 5.1.31 and 5.1.32 respectively; issuance and renewal still require
  operator verification.
- A separate Protect endpoint. Confirm its hosting console, UniFi OS version,
  and native Let's Encrypt/Cloudflare availability before using the same
  issuance procedure there; the Protect application version alone is not enough.
- A registered domain whose authoritative DNS is hosted by Cloudflare.
- Access to manage that zone's API tokens and the consoles' certificate settings.
- A local DNS service used by the management workstation and recovery clients.
- Working outbound DNS and HTTPS from each console, with correct system time.

Use production Let's Encrypt certificates for normal browser access. This guide
does not configure a staging issuer or require public inbound ports or Cloudflare
proxying. DNS-01 proves domain control through public TXT records; the management
service itself stays private.

Certificate configuration belongs to the hosting console, not each application
version. This setup treats Network on the UDM, Protect, and Drive on the UNAS as
three separate HTTPS endpoints. Configure Protect on its own hosting console;
do not assume the UDM certificate covers it.

## Choose names and local DNS records

The following names and documentation addresses are synthetic placeholders.
Replace them with the selected domain and the consoles' actual private addresses.

| Console | Certificate domain and browser hostname | Local DNS A record |
| --- | --- | --- |
| UDM SE | `udm.example.com` | `192.0.2.10` |
| Protect hosting console | `protect.example.com` | `192.0.2.30` |
| UNAS Pro | `nas.example.com` | `192.0.2.20` |

Keep these direct UniFi names outside Caddy's dedicated `infra.example.com`
namespace. Caddy uses `*.infra.example.com`, including for
`modem.infra.example.com` and `room-alert.infra.example.com`; that wildcard
cannot authenticate the UniFi names above. The namespace boundary does not
reduce the DNS permissions of tokens scoped to the shared Cloudflare zone.

Use a single hostname per console for the initial setup. Avoid issuing another
copy of Caddy's wildcard. Specific certificate names will be visible in public
Certificate Transparency logs.

For clients using Pi-hole, manually add all three hostname/address pairs in
Pi-hole's local DNS records. Use the Protect endpoint's own private address for
its record. If multiple Pi-hole instances serve clients, keep the records
consistent on each instance. Confirm each hostname resolves to the intended
address from a management client.

If clients instead use Technitium, configure the records there. If they use
UniFi DNS, use the Network DNS Records interface described in the linked vendor
guide. A record configured on a DNS server that clients bypass will not change
their results. Retain public resolution for ACME challenge records when using
local overrides.

The local records point directly to the consoles, not Caddy. Public A or AAAA
records for these private management endpoints are unnecessary for DNS-01.
Do not enable port forwarding to obtain these certificates.

Later private IP changes require updating local DNS and any affected routes or
firewall rules. A certificate reissue is unnecessary while its hostname remains
the same. Direct IP URLs do not match these hostname certificates.

## Create one Cloudflare token per console

1. Open Cloudflare **My Profile > API Tokens > Create Token**.
2. Choose **Create Custom Token**, or adapt the **Edit zone DNS** template.
3. Give the token a descriptive name, such as `unifi-udm-acme`.
4. Configure these permissions:

   | Scope | Resource | Permission |
   | --- | --- | --- |
   | Zone | DNS | Edit |
   | Zone | Zone | Read |

5. Under **Zone Resources**, select **Include > Specific zone** and choose only
   the zone containing the certificate hostname, such as `example.com`.
6. Review any client IP restrictions and expiry. An IP restriction must match
   the console's public egress address, not its private LAN address, and must
   account for WAN changes. If an expiry is set, arrange rotation before it.
7. Review the summary and create the token. Save its value in an independently
   recoverable password manager; Cloudflare displays the value only once.
8. Repeat for the UNAS with a separate token, such as `unifi-unas-acme`, and for
   the confirmed Protect hosting console with `unifi-protect-acme`.

These are the proposed restricted-zone permissions for this setup, consistent
with the referenced Cloudflare ACME provider documentation. They are not a
vendor-published minimum permission contract for UniFi's internal implementation.
Confirm successful issuance with these permissions before treating them as
validated for the installed versions. Do not fall back to a global API key or
all-zone access if issuance fails.

DNS Edit applies to records throughout the selected zone, not only
`_acme-challenge`. Separate tokens permit independent revocation and rotation;
they do not provide record-level isolation within the same zone. None of these
tokens belongs in CI, Forgejo Runner, or the NUC #4 certificate issuer.

## Issue and activate the UDM certificate

1. Sign in directly to the UDM using its existing management access.
2. Open **Settings > Control Plane > Console > Certificates**. Menu labels may
   vary; open **Add New Certificate** and select **Let's Encrypt**.
3. Complete the form:

   | Field | Value |
   | --- | --- |
   | Name | `UDM management` (display label) |
   | Domain Name | `udm.example.com` (replace with the chosen hostname) |
   | Manual DNS Setup | Unchecked |
   | DNS Provider | Cloudflare |
   | API Token | The dedicated UDM token, entered directly from the password manager |

4. Select **Add**. This starts a live certificate request and DNS challenge.
   The observed UniFi form advises allowing up to 15 minutes for issuance.
5. Wait for the certificate to show successful issuance. If the certificate list
   offers a separate activation action, select the new certificate there.
6. Visit `https://udm.example.com` from a client using the configured local DNS.
   Check the certificate shown by the browser: hostname coverage, trusted issuer,
   and validity dates. A successful request in the console alone does not prove
   the web server is serving the new certificate.
7. Test login, MFA if enabled, and Network navigation. Retain the existing direct
   access route until these checks pass. Verify the separate Protect endpoint
   using the procedure below.

Do not submit repeated certificate requests while one is pending. If issuance
fails, check token permissions and zone scope, token expiry, public challenge
resolution, any existing challenge delegation, applicable CAA records, console
time, and outbound connectivity. Inspect errors locally without sharing tokens
or full credential-bearing logs.

## Configure the separate Protect endpoint

Confirm the Protect hosting console exposes the native Let's Encrypt form with
Cloudflare. If it does, repeat the console procedure there using
`Protect management`, `protect.example.com` (replace with the chosen hostname),
and the dedicated Protect token. Issue and activate the certificate on that
console, not on the UDM.

Visit `https://protect.example.com` through Pi-hole's local DNS record. Confirm
the certificate served covers that hostname, then test login, MFA if enabled,
live view, and recording playback. A DNS record alone does not install a
certificate or make a certificate for `udm.example.com` cover Protect.

If native issuance is unavailable on the Protect console, retain its existing
access and resolve its deployment method separately. Do not assume the reported
UDM or UNAS OS version applies to this endpoint.

## Repeat for the UNAS

Repeat the console procedure on the UNAS, using `NAS management`, the selected
NAS hostname, and its dedicated token. Confirm the served certificate and test
Drive login, folder navigation, and a small upload/download using disposable
test data. Delete that test data afterward.

The browser certificate does not configure SMB, NFS, or every native-client
connection. Validate those services separately if their settings change.

## Renewal, rotation, and recovery

Leave the automatic provider configuration and its token available to UniFi.
Manual DNS setup would require operator handling of challenge records and is
not the automatic path described here.

Record the initial certificate serial or fingerprint and expiry in private
operator records. After a later automatic renewal, inspect the certificate
served by the hostname again and confirm a replacement with later validity.
Do not claim renewal is proven by initial issuance. Check certificate status
periodically and after OS updates; do not assume failure notifications are
configured merely because automatic issuance is available.

For token rotation, create a replacement with the same restricted zone access,
update the console's provider configuration using the available UI, and verify
that the replacement credentials can complete issuance before revoking the old
token. Editing an installed certificate's provider settings may differ by OS
version; inspect the UI rather than deleting a working certificate first.

Keep console access, Cloudflare account recovery, token records, chosen hostnames,
and local DNS recovery available independently of NUC #4, Kubernetes, Forgejo,
and Semaphore. Do not assume a console backup contains reusable certificate
keys or provider credentials unless a restore has demonstrated that behavior.
After a console replacement, restore network access and DNS, recreate a token
if needed, request a certificate through the native UI, and verify the served
certificate and application workflows again.

## Evidence and references

Repository validation covers documentation quality and repository safeguards.
It does not log in to consoles, create Cloudflare tokens, issue certificates,
or prove automatic renewal. Those remain separate operator evidence.

- [Ubiquiti certificate settings](https://help.ui.com/hc/en-us/articles/34210126298775-Self-Hosting-UniFi)
- [Dream Machines OS 5.1.12: automatic Let's Encrypt support](https://community.ui.com/releases/6a229a80-ed47-4509-a9a3-046122ecc1b9)
- [Network Attached Storage OS 5.1.8: automatic Let's Encrypt support](https://community.ui.com/releases/499f9b89-e4fb-45a3-949e-20f0656a0417)
- [UniFi DNS records and local hostnames](https://help.ui.com/hc/en-us/articles/15179064940439-UniFi-DNS-Records-and-Local-Hostnames)
- [Create a Cloudflare API token](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)
- [Cloudflare token permissions for the lego ACME provider](https://go-acme.github.io/lego/dns/cloudflare/)
- [ARRIS S33/S34 Web Manager](https://arris.my.salesforce-sites.com/consumers/articles/knowledge/S33-Web-Manager-Access)
