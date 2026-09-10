# Repository command lifecycle

This guide classifies repository commands by their effects and describes the
lifecycle conventions for adding or changing commands in this repository.

`AGENTS.md` remains the sole agent-policy surface. Current repository policy and
executable source take precedence over this procedural guide.

## Primary terms

| Term | Meaning |
| --- | --- |
| `validate` | Prove local source, configuration, schema, policy, or evidence correctness. |
| `verify` | Observe live or external state and prove an invariant without intentionally changing that target. |
| `apply` | Reconcile existing state when a generic reconciliation term is clearer than a purpose-specific action. |
| `bootstrap` | Perform exceptional initialization or establish required local or target capability. |
| `test` | Conduct a controlled experiment that can create, alter, or remove bounded temporary state. |

These terms distinguish local assurance, live observation, reconciliation,
initialization, and experimental evidence. Other established terms fit around
them:

- `check` is a specialized verification form, usually for drift or compliance.
- `plan`, `preflight`, and `dry-run` are stages or safeguards, not peer
  operations.
- `cleanup` is a deletion effect and transaction stage. It is also a valid
  standalone command when targeted removal is the requested result.
- precise terms such as `status`, `diagnostics`, `render`, `generate`, `sync`,
  `restart`, and `reset` remain valid when they state the requested result more
  clearly.

An approved semantic rename updates all repository-owned consumers together.
Do not retain a deprecated alias or parallel terminology.

## Workflow profiles

| Profile | Canonical term | Expected shape |
| --- | --- | --- |
| Local validation | `validate` | Read local inputs, prove correctness, and return an actionable result without live credentials or confirmation. Explicit local outputs are allowed. |
| Live or external observation | `verify`; specialized `check`, `status`, or `diagnostics` | Use bounded read access, do not intentionally mutate the target, and never fall back to broader credentials. |
| Existing-state reconciliation | `apply` or a precise action | Validate or preflight, obtain authority, confirm when proportionate, mutate, and read back the result. |
| Initialization or capability setup | `bootstrap` | Check initial state, establish the required capability, and verify it. |
| Controlled experiment | `test` | Bind temporary state to an exact run or target, collect evidence, clean up, and preserve cleanup failure separately. |
| Targeted removal | `cleanup` | Resolve an exact owned target, delete only that target, and verify absence. |

A command can have several effects. Name it for the result requested by the
caller, then choose safeguards from its actual behavior. A local command is not
automatically `validate`: if it creates and removes containers to obtain
evidence, it follows the controlled-test profile.

## Stages and safeguards

| Stage or safeguard | Meaning |
| --- | --- |
| validation | Prove source, input, schema, policy, or evidence correctness before relying on it. |
| preflight | Prove current prerequisites before an operation. Expose it separately only when it has independent value. |
| plan | Describe intended later work from current inputs. It is optional and never grants authority. |
| dry-run | Exercise the real operation or target validation path without persisting the intended mutation. |
| confirmation | Bind deliberate execution intent to useful operation, target, revision, or run context. |
| operator authorization | Supply policy authority to perform an operation. Naming and confirmation do not supply it. |
| post-verification | Read back and prove the requested result. |
| rollback or containment | Restore prior state or stop reconciliation when a failed operation can be contained safely. |
| cleanup | Remove only bounded temporary or run-owned state and keep cleanup failure visible. |

Not every operation needs every stage. Prefer embedded preflight when a separate
command would add ceremony or allow the checked state to become stale. Add a
standalone plan only when it provides real review or reuse value.

## Classify a command

Answer these questions in order:

1. What can the command affect: local files, temporary local state, a live host,
   repository settings, credentials, or another external target?
2. Which workflow profile matches the requested result?
3. Which primary or purpose-specific term describes that result?
4. Which safeguards match the actual consequence: validation, preflight, plan,
   dry-run, confirmation, post-verification, containment, or cleanup?
5. Who owns execution under `AGENTS.md`, and which credentials are allowed?
6. Which existing repository command family is comparable?

Do not add a stage for visual symmetry. Do not introduce a new term or workflow
shape when an established convention already describes the operation.

## Repository examples

### Local validation

```text
mise run validate:fast
mise run validate:ansible
```

These commands inspect repository-controlled inputs and produce local assurance.
They do not contact an inventory host or intentionally change live state.

### Controlled Molecule test

```text
mise run test:molecule -- system_maintenance/default
```

Molecule creates and removes rootless local containers to obtain executable role
evidence. It therefore uses `test`, with embedded Podman preflight, exact
container ownership, bounded cleanup, and no ordinary confirmation. The
containers are local, unprivileged, disposable test state; a confirmation token
would add friction without a proportionate safety benefit.

`mise run ci:changed` and `mise run ci` orchestrate validation and this
controlled test when their selected depth includes Molecule. Their effects are
the combined effects of the commands they run.

### GitHub protection administration

```text
mise run github-protection:check
mise run github-protection:plan
mise run github-protection:apply
```

`check` observes external settings. `plan` previews reconciliation but does not
authorize it. `apply` requires current operator authorization and its documented
repository-bound confirmation, then reads back the result.

### Playbook execution

```text
mise run playbook -- <playbook> <action> <inventory> [ansible-args...]
```

`playbook` is an operator gateway rather than a lifecycle term. Its requested
action determines the effect. Production or staging execution requires explicit
operator direction for the exact playbook, action, inventory, and extra
arguments. Observational actions remain read-only; mutating actions follow the
authority and precondition rules in `AGENTS.md`.

For example, `os inspect` follows the live-observation profile, while `os
provision` and `os maintain` follow the existing-state-reconciliation profile.
The [OS playbook README](../../playbooks/os/README.md) describes the subsystem.
The [managed host onboarding guide](../guides/managed-host-onboarding.md)
documents the exact operator interface and safeguards.

### Host TLS and proxy operations

The host Caddy helpers are internal operations, not alternate operator gateways.
`homelab-reverse-proxy apply-desired` reconciles the declared configuration and
its verified ingress binding; `reload` reopens selected certificates; `recover`
restores interrupted disk state before service startup. These are mutating
operations under an authorized provisioning, renewal, or service lifecycle.
`verify` observes state. The fixed TLS adapter's `validate` prepares and checks
a candidate without activating it; `reload` and `deactivate` mutate the managed
routes and verify the result. The issuer cannot choose these operations or their
paths. Operators continue to use `mise run playbook` with the applicable target
authorization; invoking an internal helper does not supply new authority.
`homelab-reverse-proxy install-trust` creates immutable device trust bundles from
the fixed root-owned candidate under the deployment lock. It checks an existing
name for exact contents and metadata and never replaces it.

### Dependency bootstrap

```text
mise run bootstrap
```

Bootstrap establishes the locked local controller and Galaxy capability. It
does not authorize playbook execution or install dependencies implicitly during
a later live operation. It reuses a verified Galaxy generation when the complete
`requirements.yml` content and repository overrides are unchanged. A change to
`uv.lock` or an override alone does not download Galaxy content. Override changes
copy the matching verified requirements generation and apply the current files.

By default, Galaxy generations are stored under `.cache/galaxy`. Set
`HOMELAB_GALAXY_CACHE_DIR` to an absolute persistent directory when disposable
checkouts must share them. Bootstrap validates the selected roles, collections,
versions, and overrides before updating the workspace links. A failed candidate
does not replace the previous selection or its success fingerprint. Use this
command to explicitly reinstall the current Galaxy requirements:

```text
mise exec -- uv run --frozen --no-sync python scripts/galaxy_dependencies.py --repair
```

### Secret identity bootstrap

```text
mise run secrets:bootstrap -- --backup /outside/repo/operator.age
mise run secrets:bootstrap -- --restore /outside/repo/operator.age
```

This purpose-specific bootstrap establishes or restores the repository's
operator age identity in the macOS login Keychain and verifies read-back. It
creates an encrypted recovery artifact at the exact operator-selected path
outside the repository. It reports only the public recipient. It does not
authorize inventory conversion or playbook execution.

### Secret validation and integration test

```text
mise run validate:secrets
mise run test:secrets
```

`validate:secrets` is local validation: it checks protected-file structure and
public recipient metadata without an identity or authenticated decryption.
`test:secrets` is a controlled experiment: it creates ephemeral identities and
encrypted fixtures in an isolated temporary environment, exercises the real
Ansible SOPS loading path, and removes its run-owned state. Neither command
accesses Keychain or protected inventory plaintext.

### TLS automation and test

`tls provision` through the playbook gateway installs host capability and
reconciles declared configuration. The timer is disabled by default; enabling
it authorizes ongoing DNS and certificate mutations and requires explicit
operator direction. `tls renew` performs one issuance/publication attempt and
can mutate DNS, certificate files, and Caddy runtime state. `tls verify` observes
installed metadata and TLS endpoints without issuing or reloading.

`mise run test:tls` is a controlled local experiment. It creates ephemeral
certificate authorities, temporary files, and loopback TLS listeners, then
removes them. It has no inventory target or Cloudflare credential. The
registered Molecule baseline test exercises installation in disposable Linux
containers. These tests provide offline evidence, not live renewal evidence.

### Direct secret editing

```text
mise run secrets:sops -- <sops-args...>
```

This is the repository gateway for direct SOPS operations. It confines SOPS
identity discovery to the selected retrieval command and defaults to the
repository Keychain helper. Its effect depends on the SOPS arguments; editing a
protected inventory file is an operator-owned mutation of that exact local file.

## Command and failure contracts

- Validate public arguments and registered targets before starting an operation.
- Use status `2` for invalid usage, an unknown target, or an unsupported internal
  dispatch value.
- Use status `1` for an unmet runtime precondition or a failed operation. Preserve
  signal statuses when practical.
- Print concise, actionable failures to standard error without a traceback for
  expected operator or environment errors.
- Keep external-dependency, primary assertion, cleanup, and recovery outcomes
  distinct when a workflow has those phases.
- A cleanup failure makes a controlled test fail but does not replace or hide
  the primary assertion outcome.
- Never broaden a target selector, credential, or deletion scope after a scoped
  check fails.

## Repository invariants

1. `validate`, `verify`, `check`, `plan`, `preflight`, and `dry-run` do not
   persist the intended target-state mutation.
2. `verify` and `check` are observational toward their targets. Use `test` when
   positive evidence requires deliberate temporary mutation.
3. Local validation does not require inventory credentials or contact managed
   hosts.
4. Experiments and cleanup operate only on run-owned or exactly identified state.
5. Cleanup and recovery failure remain visible separately from the primary
   assertion.
6. A standalone preflight or plan exists only when it has independent value.
7. A confirmation value records execution intent; it does not grant operator
   authorization.
8. Consequential live operations repeat safety-critical preconditions
   immediately before mutation.
9. A failed scoped permission or capability check never triggers a fallback to
   broader credentials or privileges.
10. New terminology or workflow shape requires a behavioral or safety
    distinction, not visual symmetry.

Executable Mise tasks, their implementation scripts, and subsystem guides remain
the authority for individual command behavior. This guide defines shared
classification and lifecycle conventions; it is not a second command inventory.
