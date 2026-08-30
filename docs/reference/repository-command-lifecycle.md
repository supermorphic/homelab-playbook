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
mise run ci:changed
mise run ci
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

The default platform set follows the Podman host architecture. ARM64 runs
Debian and Rocky concurrently and reports Arch as skipped. AMD64 runs Debian,
Rocky, and Arch concurrently. GitHub uses exact matrix selection on native
AMD64 so every pull request still validates all three platforms.

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

### Dependency bootstrap

```text
mise run bootstrap
```

Bootstrap establishes the locked local controller and Galaxy capability. It
does not authorize playbook execution or install dependencies implicitly during
a later live operation.

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
