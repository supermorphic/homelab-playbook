# SOPS secrets

This repository uses SOPS 3.13.2 and age 1.3.1 to protect inventory variables.
Ansible loads sibling `secrets.sops.yml` files through `community.sops` 2.4.0.
Public variables remain in `vars.yml` and `versions.yml`.

Agents and pull-request CI do not decrypt protected inventory. Only an operator
may set up or restore a live identity or edit live protected values.

## Create the operator identity

Use a path outside every checkout for the encrypted recovery copy:

```bash
mise run secrets:bootstrap -- --backup /outside/repository/operator.age
```

The setup command generates the identity without writing a plaintext identity
file. It stores the identity as a generic password in the login Keychain with
service `homelab-playbook.sops.age` and account `operator`, verifies read-back,
and prints only the public recipient. It writes a passphrase-encrypted age
backup to the exact path supplied with `--backup`.

The login Keychain must be unlocked. macOS may ask for initial access approval.
Keep the encrypted backup and its passphrase independently recoverable without
the original workstation, Semaphore, or the managed cluster.

If the identity is missing or the Keychain is locked, the helper fails. It does
not fall back to another identity. The helper also refuses to write the private
identity directly to a terminal.

## Restore on a new Mac

Install the repository dependencies, make the encrypted backup available
outside the checkout, and run:

```bash
mise install
mise run bootstrap
mise run secrets:bootstrap -- --restore /outside/repository/operator.age
```

Enter the backup passphrase when prompted. The command restores the same login
Keychain item, verifies read-back, and prints only the public recipient. Confirm
that the recipient matches a recipient authorized in `.sops.yaml` before using
the restored identity.

## Edit protected inventory

Run direct SOPS commands through the repository gateway:

```bash
mise run secrets:sops -- \
  inventory/production/group_vars/os_managed/secrets.sops.yml
```

Use the equivalent `secrets.sops.yml` path for staging or frozen inventory.
SOPS decrypts the editor buffer and writes only authenticated ciphertext to the
protected file. Do not place protected data in YAML keys because keys remain
visible.

If you exit the editor without changes, SOPS prints `File has not changed,
exiting.` The repository command treats this as success and leaves the encrypted
file unchanged. Actual editor, key retrieval, and encryption failures still
report a failed task.

The gateway uses a temporary `HOME` and `XDG_CONFIG_HOME` for SOPS and clears
ambient age key-file, inline-key, and SSH-key settings. This prevents SOPS from
falling back to an unintended local identity. Its temporary context contains no
secret. The selected identity command and editor run with the operator's
original home and configuration context. By default, the gateway selects the
repository Keychain helper.

Normal playbook commands need no extra secret flag. Ansible uses the configured
`age_key_cmd` and the same Keychain helper:

```bash
mise run playbook -- os inspect production
```

Production and staging execution still requires explicit operator direction
for the exact playbook, action, inventory, and extra arguments.

## Automation controllers

An automation controller runs playbooks on your behalf. Semaphore is an example:
you start or schedule a job through its web UI, and its runner executes Ansible.
The runner needs access to the encrypted variables used by that job.

Semaphore uses its own age identity and authorizes its public recipient only for
the inventory scope it needs. It never receives the Mac operator identity. The
operator enrolls the encrypted controller credential at
`/etc/semaphore/controller-age.cred` before enabling the controller. The role
does not create, read, or replace that live identity.

For a controller job that runs a playbook, use:

```bash
ANSIBLE_SOPS_AGE_KEY_CMD=/opt/homelab/identity-command.py \
  mise run playbook -- <playbook> <action> <inventory> [ansible-args...]
```

`ANSIBLE_SOPS_AGE_KEY_CMD` tells Ansible's SOPS plugin how to retrieve the
controller's private age key instead of calling your Mac's Keychain helper.
Configure this in the runner's job environment so routine jobs use it
automatically. You normally do not need this override when running playbooks
on your Mac.

`/opt/homelab/identity-command.py` reads one bounded response from the private
`/run/semaphore-identity/age.sock` socket. A host systemd socket service decrypts
the enrolled systemd credential directly into that response. The command writes
the validated identity only to the SOPS pipe. It does not use a plaintext
private-key file or an ambient age identity. The controller's public recipient
must also be added to the authorized files through the
[recipient procedure](#change-recipients); setting the command alone does not
grant decryption access.

For a direct SOPS operation using the controller's identity, use:

```bash
SOPS_AGE_KEY_CMD=/opt/homelab/identity-command.py \
  mise run secrets:sops -- <sops-args...>
```

`SOPS_AGE_KEY_CMD` configures SOPS itself. Use it for a controller-side maintenance
operation, such as editing an encrypted file or updating its recipients. This
command does not run a playbook. Routine Semaphore playbook jobs use the Ansible
setting above and do not need a separate direct SOPS command.

In these examples, each environment override applies only to the command it
prefixes. On your Mac, normal `mise run playbook` and `mise run secrets:sops`
commands already select the repository Keychain helper automatically.

Debian uses the normal host socket permissions. The identity socket remains
accessible only through the dedicated controller boundary. Offline tests
exercise systemd activation; actual host enforcement needs operator verification
on the selected host.

Credential enrollment and recipient edits remain explicit operator actions.
After creating the root-owned private `/etc/semaphore` directory, pipe the
dedicated identity from its approved protected source into this command on the
selected host:

```bash
sudo systemd-creds encrypt --with-key=host --name=semaphore-age - \
  /etc/semaphore/controller-age.cred
```

Keep the credential root-owned with mode `0600`. Do not put the identity in
shell arguments, a here-document, or a temporary file. The host key used by
systemd protects this credential at rest; keep the original controller identity
in independent protected recovery storage. Replacement-host recovery enrolls
that same identity under the replacement host's key before starting the adapter.
The repository requires neither a network secret service nor a plaintext
identity file.

## Change recipients

Recipients are an OR set: any listed recipient can decrypt the files whose
metadata names it. Commit only public recipients.

The initial operator rule covers all three inventory trees. When adding a
controller with narrower access, introduce scoped rules and their matching
validation as part of that controller change.

To add a controller, add its public recipient only to the authorized inventory
scope and rewrap each existing file key in that scope. Verify authenticated
decryption with the new identity before relying on it.

To remove a recipient, remove it from the applicable rules and rotate each
file's data key, then verify the remaining identities. Rewrapping alone does not
revoke data a former recipient could already decrypt. Historical ciphertext
can remain decryptable with an old identity, so rotate the protected underlying
credentials when access must be revoked.

## Migration history

The operator completed the one-way conversion of the three former Vault files.
Current production, staging, and frozen inventory use only SOPS. The one-use
conversion helper was execution state under `.tmp/`, not a permanent command.
Specification [005](../specs/005-sops-age-secrets.md) records the migration and
verification boundary.

Recovery uses SOPS and the preserved age identity. There is no reverse-conversion
command. Pre-migration Git history and the external Vault passwords are recovery
evidence, not an active second encryption format.

## Validation

Run the static metadata check and the isolated integration test:

```bash
mise run validate:secrets
mise run test:secrets
```

Static validation does not prove ciphertext integrity. Only authenticated
decryption can do that. The integration test uses temporary identities and
fixtures, exercises Ansible's configured SOPS loading path, verifies expected
failure cases, and never calls Keychain or reads live protected values.
