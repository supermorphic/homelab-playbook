#!/bin/sh
set -u

umask 077

: "${BACKUP_ROOT:?BACKUP_ROOT must name the local backup directory}"
: "${RCLONE_REMOTE:?RCLONE_REMOTE must name the deployment remote prefix}"

LOCAL_RETENTION_DAYS=${LOCAL_RETENTION_DAYS:-7}
REMOTE_RETENTION_DAYS=${REMOTE_RETENTION_DAYS:-90}
RCLONE_REMOTE=${RCLONE_REMOTE%/}

if [ ! -d "$BACKUP_ROOT" ] || [ -L "$BACKUP_ROOT" ]; then
    echo "BACKUP_ROOT must be an existing non-symlink directory" >&2
    exit 1
fi
case "$RCLONE_REMOTE" in
    *:*) ;;
    *) echo "RCLONE_REMOTE must include an rclone remote name" >&2; exit 1 ;;
esac
case "$LOCAL_RETENTION_DAYS:$REMOTE_RETENTION_DAYS" in
    *[!0-9:]* | :* | *:) echo "retention days must be non-negative integers" >&2; exit 1 ;;
esac

now_epoch=${ARCHIVE_NOW_EPOCH:-$(date +%s)}
status=0
work_directory=

cleanup() {
    case "$work_directory" in
        /tmp/semaphore-transfer.*) rm -rf -- "$work_directory" ;;
    esac
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

archive_epoch=
archive_timestamp=
parse_archive_name() {
    archive_name=$1
    case "$archive_name" in
        semaphore-????????T??????Z-*) ;;
        *) return 1 ;;
    esac
    remainder=${archive_name#semaphore-}
    archive_timestamp=${remainder%%-*}
    suffix=${remainder#*-}
    [ -n "$suffix" ] || return 1
    case "$suffix" in *[!A-Za-z0-9]*) return 1 ;; esac
    archive_epoch=$(date -u -D %Y%m%dT%H%M%SZ -d "$archive_timestamp" +%s 2>/dev/null) || return 1
    case "$archive_epoch" in '' | *[!0-9]*) return 1 ;; esac
}

local_hash=
local_bytes=
validate_local_archive() {
    archive=$1
    [ -d "$archive" ] && [ ! -L "$archive" ] || return 1
    [ -f "$archive/database.dump" ] && [ ! -L "$archive/database.dump" ] || return 1
    [ -f "$archive/SHA256SUMS" ] && [ ! -L "$archive/SHA256SUMS" ] || return 1

    for entry in "$archive"/* "$archive"/.[!.]* "$archive"/..?*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        case "${entry##*/}" in
            database.dump | SHA256SUMS) ;;
            *) return 1 ;;
        esac
    done

    checksum_line=$(cat "$archive/SHA256SUMS") || return 1
    # Split canonical checksum fields; noglob prevents input from expanding paths.
    set -f
    # shellcheck disable=SC2086
    set -- $checksum_line
    set +f
    [ "$#" -eq 2 ] || return 1
    local_hash=$1
    [ "$2" = database.dump ] || return 1
    [ "$checksum_line" = "$local_hash  database.dump" ] || return 1
    [ "${#local_hash}" -eq 64 ] || return 1
    case "$local_hash" in *[!0-9a-f]*) return 1 ;; esac

    calculated_line=$(sha256sum "$archive/database.dump") || return 1
    set -f
    # shellcheck disable=SC2086
    set -- $calculated_line
    set +f
    [ "$#" -ge 1 ] && [ "$1" = "$local_hash" ] || return 1
    local_bytes=$(wc -c < "$archive/database.dump") || return 1
    local_bytes=$(printf '%s' "$local_bytes" | tr -d ' ')
    case "$local_bytes" in '' | *[!0-9]*) return 1 ;; esac
}

remote_is_complete() {
    remote_archive=$1
    expected_hash=$2
    expected_bytes=$3
    remote_has_valid_completion "$remote_archive" || return 1
    [ "$marker_hash" = "$expected_hash" ] && [ "$marker_bytes" = "$expected_bytes" ]
}

marker_hash=
marker_bytes=
remote_layout_is_exact() {
    remote_archive=$1
    remote_entries=$(rclone lsf --recursive --links "$remote_archive") || return 1
    old_ifs=$IFS
    IFS='
'
    set -f
    # Split newline-delimited remote paths; noglob prevents path expansion.
    # shellcheck disable=SC2086
    set -- $remote_entries
    set +f
    IFS=$old_ifs
    [ "$#" -eq 3 ] || return 1
    saw_complete=0
    saw_dump=0
    saw_sidecar=0
    for remote_entry do
        case "$remote_entry" in
            COMPLETE) saw_complete=$((saw_complete + 1)) ;;
            database.dump) saw_dump=$((saw_dump + 1)) ;;
            SHA256SUMS) saw_sidecar=$((saw_sidecar + 1)) ;;
            *) return 1 ;;
        esac
    done
    [ "$saw_complete" -eq 1 ] \
        && [ "$saw_dump" -eq 1 ] \
        && [ "$saw_sidecar" -eq 1 ]
}

remote_has_valid_completion() {
    remote_archive=$1
    marker=$(rclone cat "$remote_archive/COMPLETE") || return 1
    old_ifs=$IFS
    IFS='
'
    set -f
    # Split the two canonical marker lines; noglob prevents path expansion.
    # shellcheck disable=SC2086
    set -- $marker
    set +f
    IFS=$old_ifs
    [ "$#" -eq 2 ] || return 1
    case "$1" in SHA256=*) marker_hash=${1#SHA256=} ;; *) return 1 ;; esac
    case "$2" in BYTES=*) marker_bytes=${2#BYTES=} ;; *) return 1 ;; esac
    [ "${#marker_hash}" -eq 64 ] || return 1
    case "$marker_hash" in *[!0-9a-f]*) return 1 ;; esac
    case "$marker_bytes" in '' | *[!0-9]*) return 1 ;; esac
    expected_marker="SHA256=$marker_hash
BYTES=$marker_bytes"
    [ "$marker" = "$expected_marker" ] || return 1
    dump_listing=$(rclone lsl "$remote_archive/database.dump") || return 1
    old_ifs=$IFS
    IFS=' '
    set -f
    # Split rclone's metadata fields; noglob prevents path expansion.
    # shellcheck disable=SC2086
    set -- $dump_listing
    set +f
    IFS=$old_ifs
    [ "$#" -ge 1 ] && [ "$1" = "$marker_bytes" ] || return 1
    sidecar=$(rclone cat "$remote_archive/SHA256SUMS") || return 1
    [ "$sidecar" = "$marker_hash  database.dump" ] || return 1
    remote_layout_is_exact "$remote_archive"
}

upload_and_verify() {
    archive=$1
    remote_archive=$2

    rclone deletefile "$remote_archive/COMPLETE" >/dev/null 2>&1 || true
    rclone copyto "$archive/database.dump" "$remote_archive/database.dump" || return 1
    rclone copyto "$archive/SHA256SUMS" "$remote_archive/SHA256SUMS" || return 1

    work_directory=$(mktemp -d /tmp/semaphore-transfer.XXXXXX) || return 1
    if ! rclone cat "$remote_archive/database.dump" > "$work_directory/remote.dump"; then
        cleanup
        work_directory=
        return 1
    fi
    if ! remote_checksum_line=$(sha256sum "$work_directory/remote.dump"); then
        cleanup
        work_directory=
        return 1
    fi
    set -f
    # Split sha256sum output; noglob prevents path expansion.
    # shellcheck disable=SC2086
    set -- $remote_checksum_line
    set +f
    if [ "$#" -lt 1 ] || [ "$1" != "$local_hash" ]; then
        echo "remote dump checksum verification failed: ${archive##*/}" >&2
        cleanup
        work_directory=
        return 1
    fi
    remote_bytes=$(wc -c < "$work_directory/remote.dump") || {
        cleanup
        work_directory=
        return 1
    }
    remote_bytes=$(printf '%s' "$remote_bytes" | tr -d ' ')
    if [ "$remote_bytes" != "$local_bytes" ]; then
        echo "remote dump byte count verification failed: ${archive##*/}" >&2
        cleanup
        work_directory=
        return 1
    fi
    printf 'SHA256=%s\nBYTES=%s\n' "$local_hash" "$local_bytes" > "$work_directory/COMPLETE"
    if ! rclone copyto "$work_directory/COMPLETE" "$remote_archive/COMPLETE"; then
        cleanup
        work_directory=
        return 1
    fi
    if ! remote_is_complete "$remote_archive" "$local_hash" "$local_bytes"; then
        rclone deletefile "$remote_archive/COMPLETE" >/dev/null 2>&1 || true
        cleanup
        work_directory=
        return 1
    fi
    cleanup
    work_directory=
}

for archive in "$BACKUP_ROOT"/semaphore-*; do
    [ -e "$archive" ] || [ -L "$archive" ] || continue
    archive_name=${archive##*/}
    if [ -L "$archive" ]; then
        echo "refusing symlinked archive: $archive_name" >&2
        status=1
        continue
    fi
    if ! parse_archive_name "$archive_name"; then
        echo "invalid local archive path: $archive_name" >&2
        status=1
        continue
    fi
    if ! validate_local_archive "$archive"; then
        echo "invalid local archive: $archive_name" >&2
        status=1
        continue
    fi

    remote_archive="$RCLONE_REMOTE/$archive_name"
    verified=0
    if remote_is_complete "$remote_archive" "$local_hash" "$local_bytes"; then
        verified=1
    elif [ "$archive_epoch" -lt "$((now_epoch - REMOTE_RETENTION_DAYS * 86400))" ]; then
        echo "archive exceeds remote retention and needs operator resolution: $archive_name" >&2
        status=1
        continue
    elif upload_and_verify "$archive" "$remote_archive"; then
        verified=1
    else
        echo "archive transfer or verification failed: $archive_name" >&2
        status=1
        continue
    fi

    if [ "$verified" -eq 1 ] && [ "$archive_epoch" -lt "$((now_epoch - LOCAL_RETENTION_DAYS * 86400))" ]; then
        if [ -d "$archive" ] && [ ! -L "$archive" ]; then
            chmod u+rwx "$archive" || {
                echo "cannot prepare archive for local retention: $archive_name" >&2
                status=1
                continue
            }
            if ! rm -- "$archive/SHA256SUMS" "$archive/database.dump" \
                || ! rmdir "$archive"; then
                echo "local retention failed safely: $archive_name" >&2
                status=1
            fi
        else
            echo "archive changed before local retention: $archive_name" >&2
            status=1
        fi
    fi
done

if [ "$status" -eq 0 ]; then
    if remote_directories=$(rclone lsf --dirs-only "$RCLONE_REMOTE"); then
        old_ifs=$IFS
        IFS='
'
        set -f
        for listed_name in $remote_directories; do
            archive_name=${listed_name%/}
            if parse_archive_name "$archive_name" \
                && [ "$archive_epoch" -lt "$((now_epoch - REMOTE_RETENTION_DAYS * 86400))" ]; then
                remote_archive="$RCLONE_REMOTE/$archive_name"
                if remote_has_valid_completion "$remote_archive"; then
                    if ! rclone deletefile "$remote_archive/COMPLETE" \
                        || ! rclone deletefile "$remote_archive/SHA256SUMS" \
                        || ! rclone deletefile "$remote_archive/database.dump" \
                        || ! rclone rmdir "$remote_archive"; then
                        echo "remote retention failed safely: $archive_name" >&2
                        status=1
                    fi
                else
                    echo "unsafe remote archive preserved: $archive_name" >&2
                    status=1
                fi
            fi
        done
        set +f
        IFS=$old_ifs
    else
        status=1
    fi
fi

exit "$status"
