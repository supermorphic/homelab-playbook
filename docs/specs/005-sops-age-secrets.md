# Specification 005: SOPS and age inventory secrets

Issue: [#18](https://github.com/supermorphic/homelab-playbook/issues/18)

## Encryption and loading boundary

SOPS with age is the sole active inventory encryption format. Public variables
remain separate from sibling encrypted group/host variables; preserve their
Ansible names and precedence. There is no mixed Vault/SOPS operational state or
reverse-conversion command. Recovery uses preserved age identities, not the
retired migration helper.

[Ansible configuration](../../ansible.cfg) owns loader ordering and the identity
retrieval command; [Mise](../../.mise.toml) and
[Galaxy requirements](../../requirements.yml) own pins. Direct SOPS operations
use `mise run secrets:sops -- <sops-args...>`. The wrapper isolates SOPS home and
configuration and clears ambient inline, file and SSH identities. Only the
selected retrieval command and editor receive the operator's original context.
Missing or incorrect retrieval must never fall back to another identity.

[Creation rules](../../.sops.yaml) select protected inventory and encrypt every
scalar, including nested values. Keys remain visible: use ordinary schema keys,
never sensitive identifiers or credentials as mapping keys. Commit only public
age recipients. The repository identity is dedicated, separate from other repos
and automation controllers; no network secret service or routine Vault prompt
is required.

## Independent operator recovery

The macOS helper reads only the designated generic-password item in the login
Keychain, refuses terminal stdout and emits no private diagnostics. Its selected
item is owned by [the helper](../../scripts/secrets/age-keychain.sh). The unlocked
Keychain and initial macOS access approval are prerequisites. Missing or locked
credentials fail without fallback.

Operator setup generates the identity in memory, stores it in Keychain without
private key arguments, writes only a passphrase-encrypted external backup and
checks read-back. Keep the backup and passphrase recoverable independently of
the original workstation, managed host, Semaphore, Forgejo and the cluster.
After dependency bootstrap, the minimum operator sequence is:

```sh
# Create a new dedicated identity; select a path outside every checkout.
mise run secrets:bootstrap -- --backup /outside/repository/operator.age
# On a replacement Mac, restore the existing identity instead.
mise run secrets:bootstrap -- --restore /outside/repository/operator.age
```

These are alternatives, not sequential setup steps. Enter the backup passphrase
only at the prompt. Confirm the reported public recipient matches an authorized
recipient before relying on restored access. No plaintext identity file is used.

For an authorized protected edit, open the exact selected `secrets.sops.yml`
through `secrets:sops`. The editor sees plaintext privately and writes only
ciphertext to the protected file; do not copy its contents into agent context.
An unchanged editor buffer is a successful no-op. Actual editor, retrieval and
encryption failures remain failures. Normal playbook execution uses the same
boundary without an extra secret flag.

## Controller identities

Each automation controller has its own identity and only the inventory scope it
needs. `ANSIBLE_SOPS_AGE_KEY_CMD` selects its Ansible retrieval command;
`SOPS_AGE_KEY_CMD` selects retrieval for direct SOPS operations. Setting either
does not grant access: the public recipient must also be enrolled and existing
file keys rewrapped. Never give a controller the workstation identity.

[Semaphore](009-semaphore-infrastructure-automation.md) uses a private socket
adapter that returns one bounded identity response directly into SOPS. A host
systemd service decrypts the enrolled credential; neither routine jobs nor the
adapter use a plaintext key file. The role does not create, inspect or replace
the live identity.

For separately authorized enrollment, establish root-owned private
`/etc/semaphore` on the selected host. Pipe the dedicated identity from its
approved protected source into:

```sh
sudo systemd-creds encrypt --with-key=host --name=semaphore-age - \
  /etc/semaphore/controller-age.cred
```

Keep the credential root-owned, mode `0600`. Never supply the identity in shell
arguments, a here-document or temporary file. Keep the original identity in
independent protected recovery storage: the encrypted host credential alone is
not portable. A replacement host must enroll that same identity under its own
host key before the socket adapter starts. Verify socket ownership and selected
host enforcement before enabling controller jobs.

## Recipient changes and revocation

Recipients form an OR set: each can decrypt its authorized files. Scope controller
rules to required inventory and keep validation consistent with those rules.
Adding a recipient requires rewrapping existing file keys and verifying
actual authenticated decryption with that identity before relying on it.

Removal requires removing the recipient and rotating each file's data key, then
verifying retained identities. Rewrapping alone does not revoke previously
accessible data. Historical ciphertext remains decryptable by former recipients;
rotate underlying credentials separately when access must be revoked. Recipient
and credential mutations remain explicit operator actions.

## Evidence boundary

Agents and PR CI never decrypt protected production, staging or frozen inventory.
Static validation checks structure and public recipient metadata, not ciphertext
integrity; only authenticated decryption proves that. Exclude protected inputs
from diagnostics that could quote values. Public inventory mirrors are explicit
and never copy protected files.

The registered `test:secrets` workflow uses disposable identities and synthetic
fixtures through the actual SOPS/Ansible loading path. It proves variable merging,
host precedence, templating, rejection of missing/wrong identities and modified
ciphertext, and isolation from ambient identity sources. It never calls Keychain.
Ephemeral test keys may exist in private temporary directories with cleanup;
that exception does not permit plaintext live identity files. Live Keychain,
controller enrollment and recovery acceptance remain separate operator evidence.
