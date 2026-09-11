from __future__ import annotations

import hashlib
from pathlib import Path

from archive_test_utils import (
    REPO_ROOT,
    ShellScriptTestCase,
    complete_text,
    create_archive,
)


class TransferTests(ShellScriptTestCase):
    script_path = REPO_ROOT / "roles/semaphore/files/transfer.sh"
    now = 1_775_952_000  # 2026-04-12T00:00:00Z

    def setUp(self) -> None:
        super().setUp()
        self.backup_root = self.root / "backups"
        self.remote_root = self.root / "remote"
        self.backup_root.mkdir()
        self.remote_root.mkdir()
        self.rclone_log = self.root / "rclone.log"
        self.executable(
            "rclone",
            r'''
command=$1
shift
if [ "${RCLONE_UNAVAILABLE:-0}" = 1 ]; then
    echo "SMB unavailable" >&2
    exit 1
fi
map_remote() {
    remote_path=${1#*:}
    printf '%s/%s\n' "$FAKE_RCLONE_ROOT" "$remote_path"
}
case "$command" in
    copyto)
        source=$1
        destination=$(map_remote "$2")
        mkdir -p "$(dirname "$destination")"
        case "$destination" in
            */database.dump) echo "PAYLOAD_WRITE $2" >> "$RCLONE_LOG" ;;
            *) echo "META_WRITE $2" >> "$RCLONE_LOG" ;;
        esac
        if [ "${FAIL_COMPLETE_ONCE_FILE:-}" ] && [ "${destination##*/}" = COMPLETE ] && [ ! -e "$FAIL_COMPLETE_ONCE_FILE" ]; then
            : > "$FAIL_COMPLETE_ONCE_FILE"
            echo "marker publication interrupted" >&2
            exit 143
        fi
        cp "$source" "$destination"
        ;;
    cat)
        source=$(map_remote "$1")
        case "$source" in
            */database.dump) echo "PAYLOAD_READ $1" >> "$RCLONE_LOG" ;;
            *) echo "META_READ $1" >> "$RCLONE_LOG" ;;
        esac
        cat "$source"
        ;;
    lsl)
        source=$(map_remote "$1")
        echo "META_STAT $1" >> "$RCLONE_LOG"
        [ -f "$source" ] || exit 1
        bytes=$(wc -c < "$source" | tr -d ' ')
        printf '%s 2026-01-01 00:00:00.000000000 %s\n' "$bytes" "${source##*/}"
        ;;
    deletefile)
        target=$(map_remote "$1")
        echo "META_DELETE $1" >> "$RCLONE_LOG"
        rm -f "$target"
        ;;
    lsf)
        if [ "$1" = --dirs-only ]; then
            target=$(map_remote "$2")
            echo "META_LIST $2" >> "$RCLONE_LOG"
            for directory in "$target"/*; do
                [ -d "$directory" ] || continue
                printf '%s/\n' "${directory##*/}"
            done
        elif [ "$1" = --recursive ] && [ "$2" = --links ]; then
            target=$(map_remote "$3")
            echo "META_ENUMERATE $3" >> "$RCLONE_LOG"
            for entry in "$target"/*; do
                [ -e "$entry" ] || [ -L "$entry" ] || continue
                name=${entry##*/}
                if [ -L "$entry" ]; then
                    printf '%s.rclonelink\n' "$name"
                elif [ -d "$entry" ]; then
                    printf '%s/\n' "$name"
                    for child in "$entry"/*; do
                        [ -e "$child" ] || [ -L "$child" ] || continue
                        printf '%s/%s\n' "$name" "${child##*/}"
                    done
                else
                    printf '%s\n' "$name"
                fi
            done
        else
            echo "unsupported fake rclone lsf arguments: $*" >&2
            exit 2
        fi
        ;;
    rmdir)
        target=$(map_remote "$1")
        echo "META_RMDIR $1" >> "$RCLONE_LOG"
        rmdir "$target"
        ;;
    *)
        echo "unsupported fake rclone command: $command" >&2
        exit 2
        ;;
esac
''',
        )
        self.raw_executable(
            "date",
            """#!/usr/bin/env python3
import datetime
import os
import sys
args = sys.argv[1:]
if args == ['+%s']:
    print(os.environ['ARCHIVE_NOW_EPOCH'])
elif len(args) == 6 and args[:3] == ['-u', '-D', '%Y%m%dT%H%M%SZ'] and args[3] == '-d' and args[5] == '+%s':
    value = datetime.datetime.strptime(args[4], '%Y%m%dT%H%M%SZ').replace(tzinfo=datetime.timezone.utc)
    print(int(value.timestamp()))
else:
    raise SystemExit('unsupported fake date arguments: ' + repr(args))
""",
        )

    def run_transfer(self, **environment: str):
        return self.run_script(
            BACKUP_ROOT=str(self.backup_root),
            RCLONE_REMOTE="nas:deployment",
            FAKE_RCLONE_ROOT=str(self.remote_root),
            RCLONE_LOG=str(self.rclone_log),
            ARCHIVE_NOW_EPOCH=str(self.now),
            **environment,
        )

    def remote_archive(self, name: str) -> Path:
        return self.remote_root / "deployment" / name

    def create_remote_archive(self, name: str, content: bytes) -> Path:
        archive = self.remote_archive(name)
        archive.mkdir(parents=True)
        digest = hashlib.sha256(content).hexdigest()
        (archive / "database.dump").write_bytes(content)
        (archive / "SHA256SUMS").write_text(f"{digest}  database.dump\n")
        (archive / "COMPLETE").write_text(complete_text(content))
        return archive

    def assert_complete(self, name: str, content: bytes) -> None:
        remote = self.remote_archive(name)
        self.assertEqual((remote / "database.dump").read_bytes(), content)
        self.assertEqual((remote / "COMPLETE").read_text(), complete_text(content))

    def test_nas_outage_preserves_three_day_backlog_then_copies_all(self) -> None:
        archives = {
            "semaphore-20260409T030000Z-a1": b"day one",
            "semaphore-20260410T030000Z-b2": b"day two",
            "semaphore-20260411T030000Z-c3": b"day three",
        }
        for name, content in archives.items():
            create_archive(self.backup_root, name, content)

        unavailable = self.run_transfer(RCLONE_UNAVAILABLE="1")

        self.assertNotEqual(unavailable.returncode, 0)
        self.assertEqual(sorted(path.name for path in self.backup_root.iterdir()), sorted(archives))
        self.assertEqual(list(self.remote_root.iterdir()), [])

        available = self.run_transfer()

        self.assertEqual(available.returncode, 0, available.stderr)
        for name, content in archives.items():
            self.assert_complete(name, content)

    def test_repeat_checks_markers_without_dump_payload_io(self) -> None:
        name = "semaphore-20260411T030000Z-repeat1"
        create_archive(self.backup_root, name, b"unchanged")
        first = self.run_transfer()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.rclone_log.write_text("")

        second = self.run_transfer()

        self.assertEqual(second.returncode, 0, second.stderr)
        log = self.rclone_log.read_text()
        self.assertIn("META_READ", log)
        self.assertNotIn("PAYLOAD_READ", log)
        self.assertNotIn("PAYLOAD_WRITE", log)

    def test_stale_marker_and_corrupt_remote_dump_are_repaired(self) -> None:
        name = "semaphore-20260411T030000Z-repair1"
        local = create_archive(self.backup_root, name, b"correct payload")
        remote = self.remote_archive(name)
        remote.mkdir(parents=True)
        (remote / "database.dump").write_bytes(b"corrupt")
        (remote / "SHA256SUMS").write_text((local / "SHA256SUMS").read_text())
        (remote / "COMPLETE").write_text("SHA256=bad\nBYTES=7\n")

        result = self.run_transfer()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_complete(name, b"correct payload")

    def test_missing_or_invalid_local_checksum_never_uploads(self) -> None:
        missing = create_archive(
            self.backup_root, "semaphore-20260411T030000Z-missing1", b"missing"
        )
        (missing / "SHA256SUMS").unlink()
        corrupt = create_archive(
            self.backup_root, "semaphore-20260411T040000Z-corrupt1", b"corrupt"
        )
        (corrupt / "SHA256SUMS").write_text(f"{'0' * 64}  database.dump\n")

        result = self.run_transfer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid local archive", result.stderr)
        self.assertFalse(self.remote_archive(missing.name).exists())
        self.assertFalse(self.remote_archive(corrupt.name).exists())
        self.assertTrue(missing.exists())
        self.assertTrue(corrupt.exists())

    def test_local_unknown_nested_and_symlink_entries_are_rejected(self) -> None:
        extra = create_archive(
            self.backup_root, "semaphore-20260411T010000Z-extra1", b"extra"
        )
        (extra / "notes.txt").write_text("preserve")
        nested = create_archive(
            self.backup_root, "semaphore-20260411T020000Z-nested1", b"nested"
        )
        nested_directory = nested / "unexpected"
        nested_directory.mkdir()
        (nested_directory / "keep.txt").write_text("preserve")
        linked = create_archive(
            self.backup_root, "semaphore-20260411T030000Z-filelink1", b"linked"
        )
        (linked / "database.dump").unlink()
        outside = self.root / "outside.dump"
        outside.write_bytes(b"outside")
        (linked / "database.dump").symlink_to(outside)

        result = self.run_transfer()

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((extra / "notes.txt").exists())
        self.assertTrue((nested_directory / "keep.txt").exists())
        self.assertTrue((linked / "database.dump").is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")
        for archive in (extra, nested, linked):
            self.assertFalse(self.remote_archive(archive.name).exists())

    def test_malformed_local_sidecar_is_rejected(self) -> None:
        archive = create_archive(
            self.backup_root, "semaphore-20260411T030000Z-badsidecar1", b"payload"
        )
        (archive / "SHA256SUMS").write_text("*[bad Ring]  database.dump\n")

        result = self.run_transfer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid local archive", result.stderr)
        self.assertTrue(archive.exists())
        self.assertFalse(self.remote_archive(archive.name).exists())

    def test_failed_remote_stream_does_not_publish_complete(self) -> None:
        name = "semaphore-20260411T030000Z-stream1"
        create_archive(self.backup_root, name, b"payload")
        original = self.bin_dir / "rclone"
        source = original.read_text().replace(
            'cat "$source"\n        ;;',
            'if [ "${FAIL_DUMP_READ:-0}" = 1 ] && [ "${source##*/}" = database.dump ]; then exit 1; fi\n        cat "$source"\n        ;;',
        )
        original.write_text(source)

        result = self.run_transfer(FAIL_DUMP_READ="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.remote_archive(name) / "COMPLETE").exists())
        self.assertTrue((self.remote_archive(name) / "database.dump").exists())

    def test_failed_hash_command_does_not_publish_complete(self) -> None:
        name = "semaphore-20260411T030000Z-hashfail1"
        create_archive(self.backup_root, name, b"payload")
        self.executable(
            "sha256sum",
            'case "$1" in\n'
            '  /tmp/semaphore-transfer.*) echo "hash command failed" >&2; exit 1 ;;\n'
            '  *) printf "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5  %s\\n" "$1" ;;\n'
            'esac\n',
        )

        result = self.run_transfer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hash command failed", result.stderr)
        self.assertFalse((self.remote_archive(name) / "COMPLETE").exists())
        self.assertTrue((self.remote_archive(name) / "database.dump").exists())

    def test_interrupted_marker_publication_is_repaired_next_run(self) -> None:
        name = "semaphore-20260411T030000Z-interrupt1"
        create_archive(self.backup_root, name, b"retry payload")
        sentinel = self.root / "failed-once"

        first = self.run_transfer(FAIL_COMPLETE_ONCE_FILE=str(sentinel))

        self.assertNotEqual(first.returncode, 0)
        self.assertFalse((self.remote_archive(name) / "COMPLETE").exists())

        second = self.run_transfer(FAIL_COMPLETE_ONCE_FILE=str(sentinel))

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assert_complete(name, b"retry payload")

    def test_retention_uses_name_time_and_preserves_old_outstanding(self) -> None:
        old_verified = "semaphore-20260404T235959Z-oldlocal1"
        boundary = "semaphore-20260405T000000Z-boundary1"
        too_old_outstanding = "semaphore-20260101T000000Z-outstanding1"
        create_archive(self.backup_root, old_verified, b"old verified")
        create_archive(self.backup_root, boundary, b"boundary")
        create_archive(self.backup_root, too_old_outstanding, b"outstanding")

        result = self.run_transfer()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.backup_root / old_verified).exists())
        self.assertTrue((self.backup_root / boundary).exists())
        self.assertTrue((self.backup_root / too_old_outstanding).exists())
        self.assertFalse(self.remote_archive(too_old_outstanding).exists())
        self.assertIn("operator resolution", result.stderr)

    def test_remote_retention_is_strictly_older_than_ninety_days_and_scoped(self) -> None:
        old = self.create_remote_archive(
            "semaphore-20260111T235959Z-oldremote1", b"expired"
        )
        boundary = self.create_remote_archive(
            "semaphore-20260112T000000Z-boundary1", b"boundary"
        )
        incomplete = self.remote_archive("semaphore-20260101T000000Z-incomplete1")
        unrelated = self.remote_root / "deployment" / "family-photos"
        for directory in (incomplete, unrelated):
            directory.mkdir(parents=True)
        (incomplete / "database.dump").write_bytes(b"partial upload")
        unrelated_file = unrelated / "keep.txt"
        unrelated_file.write_text("keep")

        result = self.run_transfer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe remote archive", result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(boundary.exists())
        self.assertTrue(incomplete.exists())
        self.assertEqual(unrelated_file.read_text(), "keep")

    def test_remote_retention_rejects_extra_nested_link_and_bad_sidecar(self) -> None:
        extra = self.create_remote_archive(
            "semaphore-20260101T000000Z-extra1", b"extra"
        )
        (extra / "notes.txt").write_text("preserve")
        nested = self.create_remote_archive(
            "semaphore-20260102T000000Z-nested1", b"nested"
        )
        nested_directory = nested / "unexpected"
        nested_directory.mkdir()
        (nested_directory / "keep.txt").write_text("preserve")
        linked = self.create_remote_archive(
            "semaphore-20260103T000000Z-link1", b"linked"
        )
        outside = self.root / "remote-outside"
        outside.write_text("outside")
        (linked / "link").symlink_to(outside)
        malformed = self.create_remote_archive(
            "semaphore-20260104T000000Z-sidecar1", b"sidecar"
        )
        (malformed / "SHA256SUMS").write_text("not a checksum\n")

        result = self.run_transfer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe remote archive", result.stderr)
        self.assertEqual((extra / "notes.txt").read_text(), "preserve")
        self.assertEqual((nested_directory / "keep.txt").read_text(), "preserve")
        self.assertTrue((linked / "link").is_symlink())
        self.assertEqual(outside.read_text(), "outside")
        self.assertEqual((malformed / "SHA256SUMS").read_text(), "not a checksum\n")

    def test_symlinked_archive_is_rejected_without_following_it(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "database.dump").write_bytes(b"outside")
        link = self.backup_root / "semaphore-20260411T030000Z-linked1"
        link.symlink_to(outside, target_is_directory=True)

        result = self.run_transfer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertEqual((outside / "database.dump").read_bytes(), b"outside")
        self.assertFalse(self.remote_archive(link.name).exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
