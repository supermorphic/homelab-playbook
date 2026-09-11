#!/bin/sh
set -eu

umask 077

: "${BACKUP_ROOT:?BACKUP_ROOT must name the local backup directory}"

if [ ! -d "$BACKUP_ROOT" ] || [ -L "$BACKUP_ROOT" ]; then
    echo "BACKUP_ROOT must be an existing non-symlink directory" >&2
    exit 1
fi

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
staging=$(mktemp -d "$BACKUP_ROOT/.partial-semaphore-$timestamp-XXXXXX")

cleanup() {
    case "$staging" in
        "$BACKUP_ROOT"/.partial-semaphore-*)
            if [ -d "$staging" ] && [ ! -L "$staging" ]; then
                chmod u+rwx "$staging" 2>/dev/null || true
            fi
            rm -rf -- "$staging"
            ;;
    esac
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

pg_dump --format=custom --compress=9 --no-owner --no-privileges \
    --file="$staging/database.dump"
pg_restore --file=/dev/null "$staging/database.dump"
(cd "$staging" && sha256sum database.dump > SHA256SUMS)

archive_name=${staging##*/}
completed="$BACKUP_ROOT/${archive_name#.partial-}"
if [ -e "$completed" ] || [ -L "$completed" ]; then
    echo "completed archive already exists: ${completed##*/}" >&2
    exit 1
fi
chmod 0400 "$staging/database.dump" "$staging/SHA256SUMS"
chmod 0500 "$staging"
mv "$staging" "$completed"
staging=
