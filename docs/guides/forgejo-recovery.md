# Forgejo recovery

Recover one exact NAS archive into disposable storage from the workstation.
The experiment validates a replacement database and application in isolation.
Production replacement and cutover require a separate operation. This procedure
remains usable when Forgejo is unavailable.

## Retained recovery material

Keep a reviewed repository revision matching the archive's image pins, the exact
completed archive identifier, private NAS access, protected settings for that
generation, and an independent expected-state record with its original synthetic
user credential. Retain matching SOPS inputs independently through the
[secret recovery procedure](sops-secrets.md). Later key rotation does not replace
the keys needed by older archives.

An archive such as `forgejo-20260930T033000Z-ab12cd34` contains `database.dump`,
`files.tar.gz`, `manifest.json` and `SHA256SUMS`. Remote publication adds
`COMPLETE`, binding all four payloads. Selection has no latest alias or fallback.
GitHub mirrors contain code; database/files archives recover collaboration state,
attachments, LFS and application credentials. NAS storage is not off-site storage.

Use an operator-owned settings directory with mode `0700`. Each file must be
private, regular, owned by the controller user and mode `0600`:

| File | Contents |
| --- | --- |
| `generation.json` | JSON with `schema: 1` and `recovery_generation` matching the manifest |
| `database-password` | Original application database password |
| `secret-key` | Original `forgejo_secret_key` |
| `internal-token` | Original `forgejo_internal_token` |
| `lfs-jwt-secret` | Original `forgejo_lfs_jwt_secret` |
| `oauth2-jwt-secret` | Original `forgejo_oauth2_jwt_secret` |

Keep private values out of repository artifacts, command arguments, history and
test output. The engine compares retained keys with archived bytes before startup.

## Independent expected state

Prepare a private JSON record before backup from a harmless recovery probe
repository. Keep its original token or password in a separate private file,
without a trailing newline. Record the archived revision and application objects:

| Field | Required value |
| --- | --- |
| `schema` | Integer `1` |
| `username`, `repository` | Original user and probe repository names |
| `credential_file` | Absolute path to the retained original token or password |
| `refs` | Map of every expected branch, tag and pull ref to its exact Git hash |
| `issue` | Object with `id`, `title`, `body` |
| `pull_request` | Object with `id`, `title`, `head` (exact head commit hash) |
| `comment` | Object with `id`, `body` on the expected issue |
| `attachment` | Object with `id`, `sha256` on the expected issue |
| `lfs` | Object with `path` (root-level probe filename), `size`, `sha256` |

Record artifact hashes from original bytes independently. Do not derive expected
values from restored data or replace authentication to make the drill pass.
The restored probe must also accept and fetch a fresh temporary commit.

## Registered fixture and attended drill

`mise run test:forgejo -- fixture` creates synthetic upstream applications and
SMB storage, seeds the independent oracle, uses production capture/transfer
engines and restores exactly one generation. It checks outage backlog recovery,
unchanged-transfer efficiency and retained outbound metadata under isolation.

An attended drill requires authorization for the exact archive, NAS prefix and
temporary recovery action. Immediately before execution, recheck matching
settings, controller revision, expected state and available capacity. The
destination must be new, absolute and under this checkout's `.tmp/forgejo`; it
must not name active storage. Use a new 8–32-character alphanumeric run identifier
and repeat it as confirmation. Confirmation binds intent and grants no authority.

Replace every placeholder with the reviewed selection:

```sh
mise run test:forgejo -- restore --attended \
  --archive-id forgejo-YYYYMMDDTHHMMSSZ-SUFFIX \
  --remote nas:approved-forgejo-prefix \
  --rclone-config /private/path/rclone.conf \
  --settings-dir /private/path/matching-generation \
  --expected-state /private/path/expected-state.json \
  --destination /absolute/checkout/.tmp/forgejo/new-drill \
  --run-id RecoveryDrill20260930 \
  --confirm-restore-experiment RecoveryDrill20260930
```

Retrieval finishes and its egress-enabled client is removed before restored
software starts. PostgreSQL has `network=none`; Forgejo shares only that loopback
namespace and publishes no host port. The containers also share the database's
rootless user namespace so private recovery files remain owned and readable by
the controller. Restore scheduling and webhooks are disabled as an additional
safeguard. Validation rejects unsafe paths, links,
special files, incompatible pins/layouts, missing keys and insufficient capacity.
Archive processing permits at most 100,000 filesystem entries, including parent
directories, 256 GiB of contents and 257 GiB of decompressed tar data. Capacity
checks include file-block rounding, 8 KiB per entry and available inodes. A larger
dataset needs reviewed controller limits before capture or recovery. Destinations
must remain under this checkout's `.tmp/forgejo` without parent traversal or links.
PostgreSQL restore uses first-error handling, one transaction and controlled
ownership/privileges. Acceptance checks original authentication, application
objects, exact refs, attachment/LFS bytes and fresh push/fetch.

Accept only when assertions and owned cleanup pass. Cleanup checks resource
labels and destination ownership, preserves the source archive and reports
cleanup failure separately. Do not substitute sources or broaden cleanup after
failure. Offline LFS checks use native protocol, bytes and Git pointers; actual
git-lfs client behavior needs attended evidence.

## Administrator recovery

Use another retained local administrator to recover an account through trusted
HTTPS. Neither SMTP nor an external identity provider is required.

If no administrator credential remains usable, authorize host-local recovery
for the exact installation and account. The pinned CLI supports a temporary
administrator through `admin user create --admin --random-password`. Capture
output directly into a private operator file and use the credential through
trusted HTTPS. Recover the intended account, remove the temporary account and
retire its credential. Review existing sessions and tokens during that action.
Keep passwords out of CLI arguments: the pinned `admin user change-password`
interface has no password-file or stdin option.

Provisioning preserves existing administrators. Missing bootstrap completion
evidence needs operator recovery; changing the protected initial password does
not reset an existing account. Preserve interrupted bootstrap records until
their identity and installed account are reconciled.

## Replacement and upgrades

Preserve old state and validate a replacement in isolation before authorizing
cutover. Review restored mirrors and webhooks before permitting egress. Prepare
replacement foundation, matching protected inputs and private HTTPS through
existing workflows. Accept the replacement before retiring old storage. The
test never activates a production route or adopts temporary volumes as live data.

Ordinary provisioning rejects runtime pin changes. Review release compatibility
and obtain a usable pre-upgrade database/files archive before a separately
authorized application upgrade. Image rollback does not reverse database
migrations. PostgreSQL major upgrades need their own validated migration and
recovery procedure; changing the restore major is outside this drill.

## Attended service acceptance

Offline evidence covers disposable experiments and role definitions. Record
these separate host outcomes before accepting the deployment:

- Trusted private HTTPS, correct redirects and clone URLs, local administration,
  authenticated clone/fetch/push, attachment access and git-lfs transfer.
- Container restart and physical reboot persistence with declared identities,
  storage ownership and the observed proxy forwarding source.
- Consecutive real backup timers, availability recovery after failure,
  independent NAS retries and an exact real NAS restore drill.
- An authorized representative mirror with explicit source authority,
  destination visibility, protection/force-update/deletion review, initial
  enrollment and a later scheduled run. Compare exact refs at a recorded source
  revision and check required LFS objects separately.

Deployment starts without production mirror targets. Acceptance does not grant
bulk migration, competing writes, protection changes or collaboration cutover.
Issues, PRs and runners move through the separate follow-up work.
