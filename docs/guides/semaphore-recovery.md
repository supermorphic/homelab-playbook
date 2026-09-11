# Semaphore recovery

Use this procedure to recover Semaphore from one explicitly selected NAS
archive. Recovery starts from the workstation and does not depend on the
Semaphore service. The restore experiment validates a replacement database and
application in isolation. It does not alter the active service or perform a
cutover.

## Required recovery material

Obtain these items independently of the failed installation:

- this repository at a reviewed revision;
- the exact completed archive identifier;
- an rclone configuration that can read the Semaphore NAS prefix;
- the protected runtime settings retained with the archive generation,
  including the database password and stable Semaphore access-key encryption
  material; and
- the expected fixture project, template, history, and credential identifiers.

Do not copy protected values into the repository, terminal history, command-line
arguments, issue text, or test output. Put the matching settings in a private
environment file with mode `0600`. The file must define `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_DB`, `SEMAPHORE_DB_USER`, `SEMAPHORE_DB_PASS`,
`SEMAPHORE_DB_NAME`, `SEMAPHORE_ADMIN`, `SEMAPHORE_ADMIN_PASSWORD`, and
`SEMAPHORE_ACCESS_KEY_ENCRYPTION`. The PostgreSQL and Semaphore database values
must match.

An eligible archive directory has a name such as
`semaphore-20260910T031500Z-Ab12`. It contains only `database.dump`,
`SHA256SUMS`, and `COMPLETE`. Selection is exact. Do not use a `latest` alias or
substitute another archive after validation fails.

## Disposable fixture test

The registered test workflow creates synthetic SMB storage, an authenticated HTTP
target, a separate managed-host network, and private settings. It seeds a
project, Bash template, completed history record, and environment secrets before
the backup. Run it through the repository task:

```sh
mise run test:semaphore -- fixture
```

The workflow generates an exact archive identifier and a new temporary
destination, then passes both explicitly to the restore engine. The fixture
target rejects requests with no credential and with an incorrect credential.
The restored template uses its stored environment secrets to authenticate to
the target and prints the fixed acceptance marker. The test also proves
that its probe cannot reach the synthetic managed-host network.

The fixture stops transfer while SMB is absent, then transfers a three-archive
backlog after SMB recovers. A traced second transfer proves that completed,
unchanged archives do not cause a remote dump upload or content download.

The workflow retrieves only the selected archive through the pinned rclone
container. It checks the remote completion digest and byte count, the sidecar,
the downloaded SHA-256 digest, and PostgreSQL custom-archive readability before
creating the database. It then creates an internal Podman network, fresh
run-owned database volume, PostgreSQL 17 database, and Semaphore instance. The
restore uses `pg_restore` with first-error handling, one transaction, and no
restored ownership or privileges.

Cleanup runs after both success and failure. It checks each Podman ownership
label before removal, disconnects the fixture target, and removes only resources
and storage made by that invocation. A cleanup error is reported separately
from the restore error. The source NAS archive is never a cleanup target.

## Attended drill

An attended drill is a live external read and a consequential temporary
mutation. Run it only after the operator explicitly authorizes the exact archive,
NAS target, and drill action. The `--confirm-restore-experiment` value is an
execution-intent guard. It is not authorization.

Immediately before the drill:

1. Confirm the selected archive identifier and NAS prefix with the operator.
2. Confirm that the settings file belongs to the selected archive generation.
3. Confirm the target is a harmless recovery fixture and has no route to managed
   infrastructure.
4. Confirm the destination does not exist and is inside storage allocated to
   this run.
5. Recheck that the active database storage is not among the selected paths or
   Podman resources.

The selected target must be an existing harmless Podman container with the label
`io.homelab.semaphore.fixture=true`. The restore engine verifies that label before
it attaches the container to the isolated recovery network. The denied URL must
name a separate synthetic service whose reachability was proved from its own
fixture network before the drill. Failure to resolve or connect to that service
from the recovery network then proves the intended network boundary.

Invoke the registered restore mode with the exact target and repeat that target
as the confirmation value. Use placeholder values until the operator approves
the exact archive, NAS prefix, and target:

```sh
mise run test:semaphore -- restore --attended \
  --archive-id semaphore-YYYYMMDDTHHMMSSZ-SUFFIX \
  --destination /absolute/new/run-owned-destination \
  --remote nas:approved-semaphore-prefix \
  --rclone-config /private/path/rclone.conf \
  --settings-env /private/path/matching-settings.env \
  --target approved-fixture-container \
  --confirm-restore-experiment approved-fixture-container \
  --fixture-target-url http://fixture-target:8000/probe \
  --denied-url http://managed-host:8000/ \
  --expected-project-id PROJECT_ID \
  --expected-template-id TEMPLATE_ID \
  --expected-environment-id ENVIRONMENT_ID \
  --expected-history-id COMPLETED_TASK_ID \
  --expected-key-id ENVIRONMENT_SECRET_ID
```

Supply private file paths, never their contents, on the command line. Replace
the expected names and output options too if the retained fixture record uses
values other than the documented defaults.

Accept the drill only when all of these checks pass:

- the requested archive, checksum, and `COMPLETE` marker were retrieved without
  fallback;
- PostgreSQL accepted the custom archive and completed the restore transaction;
- the isolated Semaphore API returned the expected project, template, stored
  credential ID, and earlier completed task ID;
- the target rejected missing and incorrect credentials;
- the restored template completed with the expected target response without
  replacing its stored credential; and
- all run-owned containers, network attachments, network, volume, and files were
  removed while the source archive remained present.

Do not accept a drill based only on archive listing, checksum success, or mocked
unit tests. Those checks do not prove that PostgreSQL can restore the archive or
that Semaphore can decrypt and use the stored credential.

## Recover the service

Perform service recovery as a separate operator-controlled change:

1. Preserve the current database storage. Do not restore over it.
2. Select and validate one completed archive as described above.
3. Recreate the declared PostgreSQL role and database in fresh replacement
   storage. Restore with a PostgreSQL 17 client using first-error handling, one
   transaction, `--no-owner`, and `--no-privileges`.
4. Start the pinned Semaphore version with settings and stable key material that
   match the archive. Keep it isolated from the production proxy, schedules,
   managed inventories, and container runtime socket.
5. Review every recovered project, template, inventory target, schedule, and
   credential binding. Validate the expected history and run only the harmless
   recovery fixture.
6. Ask the operator to accept the replacement and authorize the exact cutover.
   A successful drill does not authorize this action.
7. Switch the application to the accepted replacement database. Recheck health,
   private ingress, and intended schedules and targets before enabling job
   execution.
8. Create and validate a fresh backup from the accepted database.
9. Keep the previous database storage until the operator explicitly accepts its
   retirement.

If validation fails, stop the replacement and preserve both the source archive
and previous database. Resolve the cause before another attempt. Do not make the
active database conform to a failed restored copy.

## Version changes

An application upgrade needs a usable pre-upgrade archive and a procedure that
accounts for Semaphore schema changes. Starting an older container against a
database already migrated by a newer version does not establish rollback.

A PostgreSQL major-version change requires its own tested migration procedure.
The PostgreSQL 17 dump and restore procedure in this guide is not evidence for
an in-place major upgrade or downgrade.
