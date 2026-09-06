# Specification 004: Managed host onboarding

Issue: [#2 Bootstrap NUC #4 Debian host with Ansible](https://github.com/supermorphic/homelab-playbook/issues/2)

## Purpose

Establish a repeatable path for adding an installed Linux host to the existing
operating-system maintenance and security baseline. Use that path to make NUC
number 4, named `nuc4`, the first active `os_managed` production host.

This work connects an installer-prepared host to the reusable baseline from
Specification 003. It adds only the missing host-identity policy, active
inventory, encrypted baseline inputs, verification, and operator procedure. It
does not deploy a container runtime or an application.

## Governing decisions

1. The installer or operator establishes initial authority before Ansible runs.
   The target already has a key-only `ansible` account, a locked account
   password, and working passwordless sudo.
2. The repository uses the existing `os provision` operation for first
   onboarding. It does not add a NUC-specific bootstrap playbook.
3. `os_managed` replaces `servers` as the reusable group targeted by OS
   playbooks and complete-baseline Molecule coverage.
4. Production initially contains only `nuc4`. Future semantic groups and hosts
   enter the active inventory when their owning initiatives make them active.
5. A focused repository-owned role manages static hostname and timezone. The
   existing read-only baseline verifier checks their effective state; no
   standalone OS verification playbook is added.
6. NUC #4 uses the static hostname `nuc4` and the timezone local to the
   machine's deployment. The public specification and inventory do not disclose
   the exact timezone.
7. The existing baseline remains the complete package policy. This initiative
   adds no convenience, development, container, or application packages. Git
   is not required on a managed host.
8. The exact timezone, complete desired SSH public-key set, and private
   management-source set live in Ansible Vault. Local operator runs use
   interactive `--ask-vault-pass`. This initiative adds no password file,
   Keychain client, password-manager integration, or repository helper for the
   Vault password.
9. The human operator may use the `ansible` login with their own authorized
   private key. This initiative does not add a second human administrator
   account or a shared private key.
10. Pull-request validation remains offline and secret-free. Live provisioning
    occurs only after merge and only after explicit authorization for the
    exact playbook, action, inventory, host limit, and arguments.
11. The source-adjacent OS README describes the playbook subsystem. The
    goal-oriented setup and operator procedure lives in
    `docs/guides/managed-host-onboarding.md`, and `docs/README.md` indexes it.

## Scope

### Included

- the manual authority and recovery prerequisites that must exist before
  Ansible can safely manage a host;
- an operator SSH configuration and Ed25519 key procedure without command-line
  authentication overrides;
- a reusable static-hostname and timezone role;
- hostname and timezone checks in the existing baseline verifier;
- replacement of the active production topology with the `os_managed` group
  and `nuc4` host;
- replacement of the production Pi-hole variable boundary with the encrypted
  `os_managed` baseline-input boundary;
- migration of OS playbooks and their complete-baseline test composition from
  `servers` to `os_managed`;
- a reusable managed-host guide with `nuc4` as the current example, including
  exact workstation commands for inventory preparation, inspection, initial
  provisioning, and live confirmation; and
- offline contract, inventory, lint, and Molecule coverage.

### Excluded

- operating-system installation and creation of initial administrative
  authority by Ansible;
- root SSH login, password-based SSH, sudo password storage, and fallback
  credentials;
- a separate human administrator login;
- a standalone read-only OS verification playbook;
- macOS Keychain, Proton Pass CLI, Vault password-client scripts, plaintext
  Vault password files, and other password-saving conveniences;
- Git, Podman, Quadlet, Forgejo, Semaphore, Forgejo Runner, TLS automation,
  application services, and their packages or ports;
- Pi-hole provisioning, migration, or deletion on a live machine;
- DNS, Plex, Kubernetes, and Tailscale runtime dependencies;
- staging hosts or speculative production group membership;
- external availability monitoring, notifications, and remote logging; and
- live production execution as part of pull-request validation.

## Authority established before Ansible

Ansible must not create the access path that grants Ansible root authority.
The Debian installer, physical console, or an already authorized operator must
establish these prerequisites first:

- Debian 13 is installed and boots normally;
- official package repositories are usable;
- the `ansible` account exists with a home directory and interactive shell;
- the operator's public key is present in the account's `authorized_keys`;
- `ansible` can run non-interactive passwordless sudo;
- the `ansible` account password is locked; and
- the operator retains Debian rescue or install media and has confirmed access
  to the NUC boot menu.

The recovery requirement means the operator can boot trusted Debian media and
repair the installed system if SSH access is lost. It is not a request to keep
a root SSH credential or an unlocked account password.

### Operator SSH key and client configuration

Continue to use Ed25519 for the operator key. When a replacement key is needed,
create it without a custom key-derivation-round argument:

```bash
ssh-keygen \
  -t ed25519 \
  -f ~/.ssh/id_ed25519_homelab_ansible \
  -C "homelab ansible operator"
```

The operator protects the private key according to workstation policy. The
repository does not store it or define how its passphrase is cached.

Configure the workstation in `~/.ssh/config`:

```sshconfig
Host nuc4
    HostName nuc4
    User ansible
    IdentityFile ~/.ssh/id_ed25519_homelab_ansible
    IdentitiesOnly yes
```

`HostName` may use the operator's resolvable private hostname or address. A live
address must not be committed to this public repository. The stable inventory
alias remains `nuc4`.

Install the public key through the already authorized installer or account
password path:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519_homelab_ansible.pub nuc4
```

Create and validate the passwordless-sudo drop-in on the target:

```bash
ssh nuc4
sudo visudo -f /etc/sudoers.d/ansible
sudo visudo -c
sudo -n true
```

The drop-in contains exactly:

```sudoers
ansible ALL=(ALL:ALL) NOPASSWD: ALL
```

Lock and inspect the account password:

```bash
sudo passwd --lock ansible
sudo passwd --status ansible
exit
```

Confirm both the unprivileged login identity and the non-interactive root path:

```bash
ssh nuc4 'id -un'
ssh nuc4 'sudo -n id -u'
```

The first command must print `ansible`. The second command must print `0`.
These checks use the named SSH client configuration and do not repeat
`PreferredAuthentications`, `PasswordAuthentication`, or
`KbdInteractiveAuthentication` options on each command.

After the baseline is applied, OpenSSH accepts only the authoritative keys for
the `ansible` account and does not accept password or direct root login. The
operator can use the same named SSH connection for exceptional maintenance.
Each additional controller or operator receives a distinct key in the complete
desired key set; private keys are never shared.

## Inventory design

Replace the current active production inventory with this initial topology:

```yaml
---
all:
  children:
    os_managed:
      hosts:
        nuc4:
```

Do not retain the old `servers`, `pihole`, or local `ansible` groups. The old
`p1` through `p4` entries are not active targets in the updated repository.
Removing their inventory does not authorize or perform any change on those
machines.

The intended long-term topology adds semantic membership as machines become
active:

```yaml
---
all:
  children:
    os_managed:
      hosts:
        nuc4:
        dns1:
        dns2:
        semaphore1:

    ci_runners:
      hosts:
        nuc4:

    dns_servers:
      hosts:
        dns1:
        dns2:

    automation_controllers:
      hosts:
        semaphore1:
```

This example defines direction, not current state. This initiative must not add
`dns1`, `dns2`, `semaphore1`, `ci_runners`, `dns_servers`, or
`automation_controllers` to production. The initiatives that activate those
functions will reconcile the example with their final deployment design.

Public `os_managed` variables define the connection account. The host variable
defines the explicit static hostname:

```yaml
---
ansible_user: ansible
```

```yaml
---
host_identity_hostname: nuc4
```

The inventory host alias and desired hostname are explicit separate inputs.
This permits a future inventory alias or resolvable address to differ from the
operating system's static hostname without an implicit rename.

## Vault input and command behavior

The complete desired SSH public-key set is required because the existing
security baseline authoritatively manages the `ansible` account's
`authorized_keys`. The complete private management-source set is required
because the baseline authoritatively limits inbound SSH through firewalld. The
desired timezone is also protected to avoid publishing the machine's deployment
location.

Create the new encrypted group input with the pinned controller tool:

```bash
mise exec -- ansible-vault create inventory/production/group_vars/os_managed/vault.yml
```

The encrypted document contains these variables with real operator-approved
values instead of the marked placeholders:

```yaml
---
host_identity_timezone: "<IANA timezone local to the machine deployment>"
security_baseline_authorized_keys:
  - "<complete desired SSH public key>"
security_baseline_management_sources:
  - "<private management CIDR>"
```

Do not place the protected values in public inventory, documentation, commands,
issue text, pull-request text, or CI output. Agents and CI treat the encrypted
file as opaque and never decrypt or inspect its protected values.

Offline validation reads only repository metadata and the first line of each
registered Vault file. It requires a regular, non-symlink file with a valid
Ansible Vault header and rejects plaintext at a protected path. This format
guard complements Gitleaks because a generic secret scanner cannot prove that
every protected value is encrypted.

The operator supplies the Vault password interactively for production OS
operations:

```bash
mise run playbook -- os inspect production --limit nuc4 --ask-vault-pass
mise run playbook -- os provision production --limit nuc4 --ask-vault-pass
mise run playbook -- os maintain production --limit nuc4 --ask-vault-pass
```

No playbook calls a credential helper. The canonical runner forwards
`--ask-vault-pass` to `ansible-playbook`. The runner continues to reject SSH and
sudo password prompts for mutating OS operations because those would violate
the established key-only, passwordless-sudo authority boundary.

Issue #4 will separately design Semaphore's encrypted credential storage and
task attachment. This initiative does not make a personal password manager or
workstation credential store available to Semaphore.

## Reusable host identity

Add a focused `host_identity` role with two required inputs:

- `host_identity_hostname`, the exact static hostname; and
- `host_identity_timezone`, an IANA timezone name.

The role validates both values before changing either. It manages the static
and effective hostname through Ansible's hostname mechanism and manages the
effective timezone through the already pinned `community.general` collection.
It uses the supported operating system's existing timezone database and adds no
new Galaxy dependency or convenience package.

The role supports the complete-baseline Debian 13 and Rocky Linux 9 contract,
although this initiative activates only Debian 13. It remains independent of
NUC-specific hardware and future application roles.

`os provision` runs host identity after bootstrap has established Python and
gathered facts, and before package and security-policy reconciliation. Repeated
runs are idempotent. `os maintain` does not reapply host identity because
maintenance observes the established baseline rather than reconciling it.
Detected identity drift directs the operator to rerun `os provision`.

## Verification and failure behavior

Extend the existing `os_baseline_verify` role with required expected-hostname
and expected-timezone inputs. It reads effective state and asserts:

- the static and current hostname equal the desired hostname; and
- the effective system timezone equals the desired IANA timezone.

Provisioning passes the host-identity inputs to the verifier after all changes
and after any Ansible-controlled reboot. Maintenance passes the same expected
inputs during its existing post-update verification. No new `os verify`
operator action is created.

Invalid or empty identity inputs fail before identity mutation. A missing Vault
password fails before Ansible can use the encrypted baseline inputs. A failed
access, repository, update, identity, security, reboot, or verification step
stops the one-host batch. After the operator corrects the reported cause, they
rerun `os provision`; they do not use `os maintain` to finish incomplete
onboarding.

The final live confirmation is:

```bash
ssh nuc4 'hostnamectl --static'
ssh nuc4 'timedatectl show --property=Timezone --value'
ssh nuc4 'sudo -n id -u'
```

The commands must print `nuc4`, the operator-approved timezone local to the
machine deployment, and `0`, respectively. These operator observations
complement the playbook's effective-state verifier. They do not replace the
playbook result or become pull-request CI evidence.

## Package and application boundary

NUC #4 receives the minimum packages already owned by Specification 003 for
Python and sudo management, OpenSSH, official repository trust, firewalld,
AppArmor, systemd time synchronization, persistent journald, auditd, and native
security updates. This initiative does not broaden that set.

In particular, Git is not needed on the managed host. Ansible transfers modules
over SSH and does not clone this repository on the target. Issue #3 owns Podman
and Quadlet prerequisites. Issue #4 owns Semaphore. Later issues own Forgejo,
runner, TLS, backup, and application dependencies.

NUC #4 must remain independent of Kubernetes, Plex, DNS, and Tailscale for its
boot, administrative access, updates, and baseline verification. Its controller
may resolve `nuc4` through local workstation configuration; the host does not
depend on an application deployed by this initiative.

## Retired production Pi-hole boundary

Delete `inventory/production/group_vars/pihole/vars.yml` and its encrypted
`vault.yml` without decrypting or inspecting the encrypted bytes. Remove the
`pihole` group from production inventory. Update source selection, inventory
tests, and documentation so `os_managed` is the only active production variable
boundary.

Retain the existing Pi-hole playbooks and roles as repository source unless a
separate initiative removes them. They have no active production target after
this migration. No deletion, update, or other action runs against the former
Pi-hole machines.

## Testing and evidence

Offline evidence includes:

- production inventory resolves exactly the active `os_managed` host `nuc4`;
- public inventory supplies the `ansible` connection user and hostname without
  loading encrypted variables;
- encrypted-source exclusion names the new `os_managed` Vault file and no
  longer names the deleted production Pi-hole Vault file;
- every registered Vault file has a valid Ansible Vault header without
  decrypting or inspecting its protected values;
- all OS playbooks target `os_managed` and preserve their current lifecycle and
  safety controls;
- the complete-baseline Molecule scenario uses `os_managed` on Debian 13 and
  Rocky Linux 9;
- host-identity configuration converges idempotently and the independent
  verifier detects hostname or timezone drift;
- the complete provisioning role order applies identity before baseline
  reconciliation and verifies it afterward;
- no new target package or Galaxy dependency is introduced; and
- `docs/guides/managed-host-onboarding.md` gives the exact interactive Vault
  and onboarding commands while the source-adjacent OS README remains a brief
  subsystem description.

Molecule may use synthetic hostnames, the `UTC` timezone, and generated
disposable SSH keys. It must not read the production Vault file or contact
`nuc4`. Container checks prove file, command, task-order, and idempotence
contracts only. They do not prove a physical hostname transition across boot,
wall-clock timezone behavior, network reachability, firewall enforcement, or
recovery-media access.

Run the repository workflows while implementing:

```bash
mise run validate:fast
mise run validate:ansible
mise run test:molecule -- system_maintenance/baseline
mise run ci:changed
```

The change-directed classifier may require additional registered validation.
It sets the minimum depth; implementation may escalate to `mise run ci` but may
not skip required work.

## Implementation sequence

1. Update inventory, source-selection, playbook, and Molecule contract tests to
   express the `os_managed` and host-identity requirements.
2. Add the reusable `host_identity` role and extend the existing baseline
   verifier.
3. Compose identity into `os provision` and pass its expectations through
   provisioning and maintenance verification.
4. Migrate OS playbooks and the complete-baseline Molecule scenario from
   `servers` to `os_managed`.
5. Replace the production inventory and public variables with the active
   `nuc4` structure.
6. Have the operator create the opaque encrypted `os_managed` Vault input, then
   remove the production Pi-hole variable directory without inspecting its
   encrypted file.
7. Put the operator procedure in the managed-host onboarding guide, link it
   from the documentation index and source-adjacent OS README, and run all
   required offline validation.
8. Merge only through the approved feature-branch workflow.
9. After merge, repeat the live prerequisites and obtain explicit operator
   authorization immediately before running `os provision` against production
   with `--limit nuc4 --ask-vault-pass`.

## Acceptance criteria

Issue #2 is complete when:

1. production contains `nuc4` in `os_managed` and no former Pi-hole hosts,
   Pi-hole group, `servers` group, or local-controller group;
2. every OS playbook and complete-baseline test composition uses
   `os_managed`;
3. public inventory declares the `ansible` connection account and hostname
   `nuc4` without disclosing the deployment timezone;
4. the encrypted `os_managed` input contains the deployment-local timezone and
   complete desired authorized-key and private management-source sets, remains
   opaque to agents and CI, and passes a non-decrypting Vault-format guard;
5. no production Pi-hole variable or Vault file remains in active inventory;
6. `host_identity` configures static hostname and timezone idempotently on both
   complete-baseline test platforms without a new dependency or target package;
7. the existing verifier independently detects hostname and timezone drift
   after provisioning and maintenance, with no standalone verification
   playbook;
8. the repository stores no private key, Vault password, password client, live
   address, or plaintext protected inventory value;
9. local production commands use the canonical runner and interactive
   `--ask-vault-pass` while SSH and sudo remain non-interactive and key-only;
10. no Git, Podman, Quadlet, Semaphore, Forgejo, runner, TLS, DNS, Plex,
    Kubernetes, Tailscale, or application responsibility enters this change;
11. required offline change-directed validation passes without contacting a
    managed host or decrypting production data; and
12. after merge and fresh explicit authorization, `os provision` completes for
    `nuc4`, its included verifier passes, and the operator confirms effective
    hostname, timezone, key-only login, and passwordless sudo.
13. the managed-host guide owns the reusable operator procedure, uses `nuc4`
    only as the current example, and the source-adjacent OS README remains a
    concise description of the playbook subsystem.
