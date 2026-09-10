# Private device browser access through Caddy

The selected [hybrid TLS design](../specs/007-off-cluster-tls-trust.md) includes
private HTTPS browser access to device management interfaces through the shared
Caddy proxy on NUC #4. The host issuer and certificate publication capability
are implemented with automatic renewal disabled by default; see the
[TLS playbook README](../../playbooks/tls/README.md). The TLS role installs the
Caddy adapter; live device routes require operator configuration and deployment.
Use this guide for their configuration and
acceptance procedure; it does not claim operational routes.

Use one explicit route per device. Caddy presents the separately managed public
certificate to browsers, then opens an independent connection to the device.
Select and verify that backend connection according to the device's actual
capabilities. This guide uses two examples:

- An ARRIS S34 cable modem with an HTTPS management interface.
- A Room Alert 3E environmental monitor with an HTTP port 80 management
  interface.

## DNS and routing

These values are synthetic examples, not production inputs:

| Device | Browser hostname | Pi-hole local A record | Backend |
| --- | --- | --- | --- |
| ARRIS S34 | `modem.infra.example.com` | `modem.infra.example.com` to `192.0.2.40` | `https://192.0.2.50:443` |
| Room Alert 3E | `room-alert.infra.example.com` | `room-alert.infra.example.com` to `192.0.2.40` | `http://192.0.2.60:80` |

The Pi-hole records point to NUC #4's private proxy listener. The backend
addresses are the devices' actual management addresses. Keep real hostnames and
addresses in private operator configuration, not in this public guide.

Keep Caddy-served names under `infra.example.com`, separate from direct UniFi
names such as `udm.example.com`, `protect.example.com`, and `nas.example.com`.
Caddy's certificate contains exactly the `*.infra.example.com` DNS SAN; it does
not include a broader wildcard or the directly managed appliance names.

Create the exact local records on each Pi-hole instance used by management
clients. Caddy must connect directly to each device, not back to its own
listener. Verify NUC #4 has a route to every device management network and
restrict proxy ingress to the intended private management networks. No public
listener or WAN port forwarding is needed.

Add one explicit hostname route per device through the proxy's Ansible-managed
configuration. Issue #25 owns the proxy runtime, configuration validation, and
reload interface; issue #5 supplies the certificate and device integration
requirements. Preserve the shared proxy's healthy routes when adding or
disabling one device route.

When a device address changes, update that route's backend and any affected
routing rules. The browser hostname and certificate can remain unchanged. When
NUC #4's listener address changes, update the Pi-hole records instead.

## Select the backend transport

### HTTPS devices

ARRIS documents its local Web Manager and browser certificate warnings, but
provides no certificate upload or automated renewal procedure in those guides.
The modem retains its existing HTTPS configuration. Caddy presents the public
certificate to browsers and uses authenticated HTTPS for the independent modem
connection.

Before enabling an HTTPS route, inspect the device certificate from a trusted
management connection. Record its issuer, validity, subject alternative names,
and fingerprint in private operator records. Confirm its identity through an
independent trusted observation before installing it as a trust anchor; merely
capturing a certificate from an unknown endpoint is not sufficient.

Configure trust specifically for that route, using Caddy's file-based TLS trust
pool and a server name that matches a certificate subject alternative name. A
private trust anchor does not bypass certificate expiry or hostname validation.
If the device has no usable certificate name, or its certificate cannot pass
validation, stop route activation and retain direct access.

Do not disable TLS verification, alter the system trust store, or silently fall
back to HTTP for a device that is expected to support authenticated HTTPS. After
a device replacement or firmware change that replaces its certificate, repeat
trust establishment before updating the route's trust material.

Declare the route and its trust bundle in protected host variables. The following
values are synthetic; replace the address, certificate name, and PEM placeholder
with independently verified device inputs before deployment:

```yaml
reverse_proxy_deferred_certificates: [infra]
reverse_proxy_routes:
  - hostname: modem.infra.example.com
    certificate_name: infra
    backend:
      transport: https
      address: 10.20.30.50
      port: 443
      server_name: modem.example.test
      trust_name: modem_v1
reverse_proxy_trust_certificates:
  modem_v1: |
    <verified PEM trust bundle>
```

`server_name` must match the device certificate. The role installs `modem_v1`
as `/etc/caddy/trust/modem_v1.pem`; only root can write it and Caddy can read it.
An existing trust name cannot receive different contents. For a replacement
certificate, add a new name such as `modem_v2` and update the route to use it.
Keeping the old bundle lets a configuration rollback retain its previous trust.

### HTTP-only devices

For the Room Alert 3E example, configure Caddy to use the explicit
`http://<device-address>:80` backend. The browser-to-Caddy connection remains
HTTPS and uses the public `*.infra.example.com` certificate. The Caddy-to-device
connection is unencrypted because this device does not provide HTTPS.

AVTECH's port reference limits HTTPS web access to the S models. The 3E user
guide documents HTTP port 80 as the default; an operator can change it under
**Settings → Advanced → General → HTTP Port**. Confirm the configured port
before declaring the backend. The v2.4.0 release notes describe a change to
data pushes, with no addition of HTTPS web access.

Treat this as a route-specific transport property. Keep the backend on a trusted
private management network, restrict access to NUC #4 and the required
management clients, and do not expose port 80 through public routing or WAN port
forwarding. Device login data and page contents are plaintext on the private hop
between Caddy and the device. Do not describe this route as end-to-end encrypted.

Do not add a TLS trust pool, TLS server name, or disabled-verification option to
an HTTP route. If a future firmware or replacement device provides HTTPS,
evaluate and test authenticated HTTPS before changing the backend transport.

Add the HTTP route to the same authoritative `reverse_proxy_routes` list:

```yaml
  - hostname: room-alert.infra.example.com
    certificate_name: infra
    backend:
      transport: http
      address: 10.20.30.60
      port: 80
```

The address is synthetic. Both examples require private listener addresses and
client networks as described in the
[proxy README](../../playbooks/reverse-proxy/README.md). With `infra` explicitly
deferred, provisioning saves these routes before the first certificate exists.
Successful certificate publication activates them together.

The proxy does not need stored administrator passwords for either transport; it
forwards the browser session. Keep device certificate material and protected
configuration outside public repository artifacts when they contain
infrastructure identifiers. Supply protected repository inputs under the
existing SOPS boundary.

## Acceptance procedure

After the operator authorizes deployment of the exact proxy configuration:

1. Validate the candidate Caddy configuration before reload. A failed candidate
   must leave the currently working configuration available.
2. Resolve each browser hostname from a client using Pi-hole. Confirm it points
   to NUC #4's intended private listener.
3. Open each HTTPS hostname and confirm the browser receives a publicly trusted
   certificate covering it, with the expected deployed certificate fingerprint.
4. Test login, logout, status pages, and normal navigation for that device.
   Confirm redirects remain usable through the hostname and do not expose or
   redirect the browser to the backend address.
5. For an HTTPS backend, confirm certificate validation remains enabled and
   successful. For an HTTP backend, confirm only the intended private port 80
   hop is used.
6. Confirm unrelated proxy routes still work.

These checks do not require a device reboot, factory reset, firmware change, or
service interruption. Do not use those actions as acceptance tests. If
redirects, cookies, or application behavior require special handling, inspect
the actual failure and add only a tested route-specific change. A trusted
browser certificate alone does not prove the entire management interface works.

## Recovery

Keep a direct management bookmark and the documented local access method for
each device available independently of Pi-hole, NUC #4, Caddy, and Internet
connectivity. Proxy access is a convenience and must not be required to restore
the device or network. A direct HTTPS URL may retain the device's existing
certificate warning; a direct HTTP URL retains its plaintext transport.

If backend trust, transport, or application compatibility fails, disable only
the affected device route and preserve other proxy services. Restore that route
only after its specific acceptance checks pass. An offline NUC #4 interrupts
proxied browser access, not the independent device function.

## References

- [AVTECH Room Alert port requirements](https://avtech.com/articles/14315/list-of-ports-required-by-room-alert-products-2/)
- [Room Alert 3E user guide, General settings on printed page 40](https://account.roomalert.com/documentation/AVTECH_Room_Alert_3E_Users_Guide.pdf)
- [Room Alert 3E firmware release notes](https://avtech.com/articles/14644/room-alert-3e-firmware-release-notes/)
- [ARRIS S33/S34 Web Manager access](https://arris.my.salesforce-sites.com/consumers/articles/knowledge/S33-Web-Manager-Access)
- [ARRIS certificate-warning guidance](https://arris.my.salesforce-sites.com/consumers/articles/knowledge/Alert-Message-for-Web-Manager-Access)
- [Caddy reverse proxy transport](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#the-http-transport)
