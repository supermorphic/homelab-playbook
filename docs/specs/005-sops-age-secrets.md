# Specification 005: SOPS and age inventory secrets

Issue: [#18](https://github.com/supermorphic/homelab-playbook/issues/18)

## Decision and scope

Replace Ansible Vault with SOPS and age for all encrypted inventory variables.
This supersedes specification 001's Vault decision and specification 004's
interactive Vault workflow. Preserve public variables, variable names, inventory
boundaries, roles, and the `mise run playbook` operator interface.

Use a dedicated repository identity, independent future controller identities,
Keychain retrieval, and a complete one-way migration.
Do not reuse the `homelab-talos` identity. No managed host changes are required.

## Loading and dependency contract

Pin community.sops 2.4.0, SOPS 3.13.2, and age 1.3.1 through Galaxy and Mise.
Explicitly enable `host_group_vars, community.sops.sops` in `ansible.cfg`, in
that order. Keep public `vars.yml` and `versions.yml` files. Encrypted variables
use sibling `secrets.sops.yml` files under group_vars or host_vars.

Configure `[community.sops] binary` to use the repository SOPS wrapper and
`age_key_cmd` to run the repository-owned Keychain helper. The wrapper gives
SOPS isolated temporary home and configuration directories and clears ambient
inline, file, and SSH age identity settings. It restores the original home and
configuration context only for the selected key command and editor; its
temporary context contains no secret. A missing or wrong selected command
cannot fall back to a default local identity.

The canonical playbook wrapper already changes to the repository root. Future
controllers override `ANSIBLE_SOPS_AGE_KEY_CMD` with their own retrieval command
and never receive the workstation identity. Direct operator SOPS operations use
`mise run secrets:sops -- <sops-args...>`; an explicit `SOPS_AGE_KEY_CMD`
overrides its default Keychain helper. No routine Vault password prompt,
plaintext identity file, or network secret service is introduced.

## Keychain and recovery

The helper reads only a generic password item from the operator's login
Keychain: service `homelab-playbook.sops.age`, account `operator`. It refuses
terminal stdout, emits no private diagnostic data, and fails when the item is
unavailable. The unlocked login Keychain and its access control are prerequisites;
initial macOS access approval may be needed. Missing or locked credentials must
not trigger a fallback to a different identity.

Operator setup generates the identity in memory, saves an age passphrase-encrypted
backup outside every checkout, stores the identity in Keychain without a private
key in command arguments, and checks read-back. Only the public recipient is
reported. Recovery restores that encrypted backup into a new Mac's login
Keychain. Keep the backup and its passphrase independently recoverable without
Semaphore, the cluster, or the original workstation.

## Recipients and encryption

`.sops.yaml` selects inventory `*.sops.yml` files and encrypts every scalar value,
including values nested in lists or mappings. YAML keys remain visible: use
ordinary schema keys, never credentials or sensitive identifiers as mapping keys.
Public recipients are the only identity material committed. The operator
supplied the dedicated recipient recorded in `.sops.yaml`; an empty recipient
list is invalid.

Recipients form an OR set: each listed identity can decrypt its authorized files.
Add future controllers only to the inventory scopes they need. Adding a recipient
requires rewrapping existing file keys; removing one also requires rotating file
data keys. Historical ciphertext remains decryptable by former recipients;
rotate underlying credentials separately when access must be revoked.

## Migration and rollback boundary

The operator completed conversion of the production os_managed, staging
semaphore, and frozen k3s_cluster group variable directories. Each `vault.yml`
was replaced by `secrets.sops.yml`; the original files were removed.

The one-use migration program remained uncommitted under `.tmp/`. It decrypted
and verified in memory, wrote only ciphertext, checked all three results,
repeated source-byte preconditions, and removed originals only after all
conversions passed. Agents tested it with synthetic files; only the operator
executed it against real inventory. The operator reported successful completion.
Agent validation checked the resulting encrypted structure and recipient metadata
without decrypting or inspecting protected values.

Before source deletion, the helper's failure path retained every original and
removed only its own candidate outputs. Deletion across directories was not
filesystem-atomic; interrupted deletion required operator reconciliation of the
verified candidates and exact remaining originals before publication.

Current recovery uses SOPS and the preserved age identity. There is no supported
mixed-format operational state or reverse-conversion command. Pre-migration Git
history and external Vault passwords remain recovery evidence, not an active
second format.

## Validation

CI never obtains production identities or decrypts real inventory. Static
validation checks encrypted structure and public recipient metadata without
claiming cryptographic integrity; only authenticated decryption can prove that.
Exclude protected files from YAML/Ansible diagnostics that could quote values.
Public inventory mirrors remain explicit and never copy protected files.

A registered `test:secrets` workflow generates ephemeral identities and encrypted
group and host fixtures. It runs Ansible locally through the actual repository
configuration, proves public/secret merging, host precedence, templated values,
and failure with a missing/wrong identity or modified ciphertext. The test uses
isolated home/config directories and a sanitized environment; it never calls
Keychain. Ephemeral test identities may exist in a private temporary directory;
this exception does not apply to live identities. Cleanup is required on failure.

Run bootstrap after dependency changes, focused validation while iterating, and
`mise run ci:changed` before completion. Live Keychain acceptance and production
conversion are separate operator evidence, never CI evidence.
