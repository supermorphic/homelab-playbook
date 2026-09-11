from __future__ import annotations

from archive_test_utils import REPO_ROOT, ShellScriptTestCase


class BackupTests(ShellScriptTestCase):
    script_path = REPO_ROOT / "roles/semaphore/files/backup.sh"

    def setUp(self) -> None:
        super().setUp()
        self.backup_root = self.root / "backups"
        self.backup_root.mkdir()
        self.log = self.root / "commands.log"
        self.executable(
            "pg_dump",
            'printf "pg_dump %s\\n" "$*" >> "$COMMAND_LOG"\n'
            'for argument do case "$argument" in --file=*) output=${argument#--file=};; esac; done\n'
            'printf "custom dump" > "$output"\n',
        )
        self.executable(
            "pg_restore",
            'printf "pg_restore %s\\n" "$*" >> "$COMMAND_LOG"\n',
        )
        self.executable(
            "sha256sum",
            'printf "sha256sum %s\\n" "$*" >> "$COMMAND_LOG"\n'
            'printf "53d87a1629c333d32f514b037f1cc20f85e853a27a55afc775c5c003b7cdb561  database.dump\\n"\n',
        )

    def run_backup(self):
        return self.run_script(
            BACKUP_ROOT=str(self.backup_root), COMMAND_LOG=str(self.log)
        )

    def assert_no_archive_state(self) -> None:
        self.assertEqual(list(self.backup_root.glob("semaphore-*")), [])
        self.assertEqual(list(self.backup_root.glob(".partial-*")), [])

    def test_publishes_one_custom_dump_after_read_and_checksum(self) -> None:
        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stderr)
        completed = list(self.backup_root.glob("semaphore-*"))
        self.assertEqual(len(completed), 1)
        self.assertRegex(
            completed[0].name, r"^semaphore-\d{8}T\d{6}Z-[A-Za-z0-9]+$"
        )
        self.assertEqual((completed[0] / "database.dump").read_text(), "custom dump")
        self.assertEqual(
            (completed[0] / "SHA256SUMS").read_text(),
            "53d87a1629c333d32f514b037f1cc20f85e853a27a55afc775c5c003b7cdb561  database.dump\n",
        )
        self.assertEqual(completed[0].stat().st_mode & 0o777, 0o500)
        self.assertEqual((completed[0] / "database.dump").stat().st_mode & 0o777, 0o400)
        self.assertEqual((completed[0] / "SHA256SUMS").stat().st_mode & 0o777, 0o400)
        self.assertEqual(list(self.backup_root.glob(".partial-*")), [])
        calls = self.log.read_text().splitlines()
        self.assertRegex(
            calls[0],
            r"^pg_dump --format=custom --compress=9 --no-owner --no-privileges --file=.+/database\.dump$",
        )
        self.assertRegex(calls[1], r"^pg_restore --file=/dev/null .+/database\.dump$")
        self.assertEqual(calls[2], "sha256sum database.dump")

    def test_failed_dump_removes_partial_output(self) -> None:
        self.executable(
            "pg_dump",
            'for argument do case "$argument" in --file=*) output=${argument#--file=};; esac; done\n'
            'printf "truncated" > "$output"\n'
            'echo "pg_dump failed" >&2\n'
            'exit 1\n',
        )

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pg_dump failed", result.stderr)
        self.assert_no_archive_state()

    def test_storage_full_during_dump_does_not_publish(self) -> None:
        self.executable(
            "pg_dump",
            'for argument do case "$argument" in --file=*) output=${argument#--file=};; esac; done\n'
            'printf "partial" > "$output"\n'
            'echo "No space left on device" >&2\n'
            'exit 1\n',
        )

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No space left on device", result.stderr)
        self.assert_no_archive_state()

    def test_unreadable_custom_dump_does_not_publish(self) -> None:
        self.executable("pg_restore", 'echo "invalid archive" >&2\nexit 1\n')

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid archive", result.stderr)
        self.assert_no_archive_state()

    def test_checksum_failure_does_not_publish(self) -> None:
        self.executable("sha256sum", 'echo "checksum failed" >&2\nexit 1\n')

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum failed", result.stderr)
        self.assert_no_archive_state()

    def test_interrupted_publication_does_not_leave_archive(self) -> None:
        self.executable("mv", 'echo "publication interrupted" >&2\nexit 143\n')

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("publication interrupted", result.stderr)
        self.assert_no_archive_state()

    def test_rejects_symlinked_backup_root(self) -> None:
        real_root = self.root / "real-backups"
        real_root.mkdir()
        linked_root = self.root / "linked-backups"
        linked_root.symlink_to(real_root, target_is_directory=True)

        result = self.run_script(BACKUP_ROOT=str(linked_root), COMMAND_LOG=str(self.log))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-symlink directory", result.stderr)
        self.assertEqual(list(real_root.iterdir()), [])

    def test_existing_completed_name_is_never_used_as_a_move_target(self) -> None:
        timestamp = "20260412T030000Z"
        completed = self.backup_root / f"semaphore-{timestamp}-fixed1"
        completed.mkdir()
        (completed / "database.dump").write_text("immutable")
        self.executable("date", f'printf "%s\\n" "{timestamp}"\n')
        self.executable(
            "mktemp",
            '[ "$1" = -d ]\npath=${2%XXXXXX}fixed1\nmkdir "$path"\nprintf "%s\\n" "$path"\n',
        )

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("completed archive already exists", result.stderr)
        self.assertEqual((completed / "database.dump").read_text(), "immutable")
        self.assertEqual(sorted(path.name for path in completed.iterdir()), ["database.dump"])
        self.assertEqual(list(self.backup_root.glob(".partial-*")), [])

    def test_termination_during_dump_cleans_partial_and_stops(self) -> None:
        self.executable(
            "pg_dump",
            'for argument do case "$argument" in --file=*) output=${argument#--file=};; esac; done\n'
            'printf "partial" > "$output"\n'
            'kill -TERM "$PPID"\n'
            'sleep 1\n',
        )

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assert_no_archive_state()


if __name__ == "__main__":
    import unittest

    unittest.main()
