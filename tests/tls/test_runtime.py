"""Fixed host operations and independent local TLS handshake evidence."""
from contextlib import nullcontext
from contextlib import redirect_stderr, redirect_stdout
import io
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import struct
import sys
import tempfile
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "roles/tls_automation/files"))
from tls_runtime import runtime
from tls_runtime.policy import parse_policy
from tls_runtime.publication import PublicationError
from test_policy import example_policy


def _acl_bytes(entries):
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", *entry) for entry in entries
    )


def _named_acl_on(path, attribute="system.posix_acl_access"):
    inode = path.lstat().st_ino
    encoded = _acl_bytes([
        (runtime.ACL_USER, 4, os.getuid() + 1000),
    ])

    def getxattr(descriptor, name):
        if os.fstat(descriptor).st_ino == inode and name == attribute:
            return encoded
        raise OSError(errno.ENODATA, "no ACL")

    return patch.object(os, "getxattr", create=True, side_effect=getxattr)


class RuntimeTests(unittest.TestCase):
    def test_renewal_requires_exact_effective_ambient_capability(self):
        contract = runtime.UNIT_CONTRACTS[runtime.RENEW_UNIT]
        self.assertEqual('cap_setuid', contract['AmbientCapabilities'])
        typed = b'a(sasbttttuii) 0\na(sasbttttuii) 0\na(sasbttttuii) 0\na(ss) 0\n'
        fragment = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_gid=0)
        for capability in ('cap_setuid', '', 'cap_setuid cap_net_admin', None):
            actual = dict(contract)
            command = actual['ExecStart']
            actual['ExecStart'] = '{ path=' + command + ' ; argv[]=' + command + ' ; ignore_errors=no }'
            actual['AmbientCapabilities'] = capability
            if capability is None:
                del actual['AmbientCapabilities']
            raw = '\n'.join(key + '=' + value for key, value in actual.items()).encode()
            with self.subTest(capability=capability), \
                    patch.object(runtime, 'run_fixed', side_effect=[raw, typed]), \
                    patch.object(runtime, 'secure_path', return_value=fragment):
                if capability == 'cap_setuid':
                    runtime.inspect_unit(runtime.RENEW_UNIT, contract)
                else:
                    with self.assertRaises(ValueError):
                        runtime.inspect_unit(runtime.RENEW_UNIT, contract)

    def setUp(self):
        self.policy = parse_policy(json.dumps(example_policy()).encode())

    def test_pinned_lego_v5_command(self):
        command = runtime.issuer_command(self.policy)
        self.assertEqual(command[:2], ["/usr/local/libexec/lego-5.4.1", "run"])
        for flag, value in [("--domains", "*.infra.example.com"), ("--dns", "cloudflare"),
                            ("--dns.resolvers", "1.1.1.1:53"),
                            ("--cert.name", "caddy"), ("--path", "/var/lib/homelab-tls-issuer"),
                            ("--server", "https://acme-v02.api.letsencrypt.org/directory")]:
            self.assertIn(flag, command)
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("--force-cert-domains", command)
        for forbidden in ["--renew-force", "--renew-days", "--ari-disable", "--deploy-hook"]:
            self.assertNotIn(forbidden, command)

    def test_load_policy_uses_only_fixed_bounded_config(self):
        encoded = json.dumps(example_policy()).encode()
        account = SimpleNamespace(pw_gid=2010)
        metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o640, st_gid=2010)
        with patch.object(runtime.pwd, "getpwnam", return_value=account), patch.object(
            runtime, "secure_path", return_value=metadata
        ) as secure, patch.object(
            runtime, "read_bounded", return_value=encoded
        ) as read:
            self.assertEqual(self.policy, runtime.load_policy())

        secure.assert_called_once_with(runtime.CONFIG)
        read.assert_called_once_with(runtime.CONFIG.parent, runtime.CONFIG.name, 0, 32768)

    def test_environment_does_not_inherit_hooks_credentials_or_trust(self):
        with patch.dict(os.environ, {"LEGO_DEPLOY_HOOK": "bad", "CF_API_KEY": "bad",
                                     "SSL_CERT_FILE": "/tmp/root", "PYTHONPATH": "/tmp"}):
            with patch.object(runtime.subprocess, "run") as run:
                run.return_value.returncode = 0
                runtime.run_fixed(["/usr/bin/systemctl", "start", runtime.ISSUER_UNIT])
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment, runtime.CLEAN_ENV)
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertIs(run.call_args.kwargs["stdout"], runtime.subprocess.DEVNULL)

    def test_issuer_failure_does_not_skip_pending_publication(self):
        publisher = Mock()
        publisher.publish.return_value = True
        issue = Mock(side_effect=RuntimeError("issuance failed"))
        snapshot = Mock(return_value=(b"chain", b"key"))
        result = runtime.reconcile(publisher, issue, snapshot)
        publisher.recover.assert_called_once_with()
        publisher.publish.assert_called_once_with(b"chain", b"key")
        self.assertEqual(result, {
            "issuance": "failed", "publication": "changed",
            "activation_failed": False, "restoration_failed": False,
        })

    def test_recovery_precedes_issuance_and_invalid_candidate_preserves_state(self):
        sequence = []
        publisher = Mock()
        publisher.recover.side_effect = lambda: sequence.append("recover")
        publisher.publish.side_effect = ValueError("invalid candidate")
        result = runtime.reconcile(publisher, lambda: sequence.append("issue"), lambda: (b"bad", b"bad"))
        self.assertEqual(sequence, ["recover", "issue"])
        self.assertEqual(result, {
            "issuance": "ok", "publication": "failed",
            "activation_failed": False, "restoration_failed": False,
        })

    def test_unrecovered_transaction_blocks_new_issuance(self):
        publisher = Mock()
        publisher.recover.side_effect = RuntimeError("rollback failed")
        issue = Mock()
        result = runtime.reconcile(publisher, issue, Mock())
        issue.assert_not_called()
        self.assertEqual(result, {
            "issuance": "not_run", "publication": "failed",
            "activation_failed": False, "restoration_failed": False,
        })

    def test_publication_error_preserves_activation_and_restoration_status(self):
        failure = PublicationError(RuntimeError("activation detail"),
                                   RuntimeError("restoration detail"))
        for phase in ("recover", "publish"):
            with self.subTest(phase=phase):
                publisher = Mock()
                if phase == "recover":
                    publisher.recover.side_effect = failure
                else:
                    publisher.publish.side_effect = failure
                result = runtime.reconcile(
                    publisher,
                    Mock(),
                    Mock(return_value=(b"chain", b"key")),
                )
                self.assertEqual("failed", result["publication"])
                self.assertTrue(result["activation_failed"])
                self.assertTrue(result["restoration_failed"])
                self.assertNotIn("detail", json.dumps(result))

    def test_failed_recovery_atomically_replaces_stale_status(self):
        with tempfile.TemporaryDirectory(prefix="tls-status-test-") as name:
            private = Path(name) / "private"
            private.mkdir(mode=0o700)
            private.chmod(0o700)
            status_path = private / "status.json"
            status_path.write_text('{"publication":"changed"}\n')
            status_path.chmod(0o600)
            publisher = Mock()
            publisher.publication_context = nullcontext
            publisher.recover.side_effect = PublicationError(
                RuntimeError("secret activation detail"),
                RuntimeError("secret restoration detail"),
            )
            with patch.object(runtime, "PRIVATE", private), patch.object(
                runtime, "STATUS", status_path
            ), patch.object(runtime.os, "geteuid", return_value=0), patch.object(
                runtime, "load_policy", return_value=self.policy
            ), patch.object(runtime, "locked", return_value=nullcontext()), patch.object(
                runtime, "inspect_host"
            ), patch.object(runtime, "validate_status_directory"), patch.object(runtime, "publisher_for", return_value=publisher):
                result = runtime.main("renew", [])

            self.assertEqual(1, result)
            status_value = json.loads(status_path.read_text())
            self.assertEqual("not_run", status_value["issuance"])
            self.assertEqual("failed", status_value["publication"])
            self.assertTrue(status_value["activation_failed"])
            self.assertTrue(status_value["restoration_failed"])
            self.assertNotIn("secret", status_path.read_text())
            self.assertEqual(0o600, stat.S_IMODE(status_path.stat().st_mode))

    def test_entrypoint_rejects_arguments_before_host_access(self):
        with patch.object(runtime, "load_policy") as load:
            self.assertEqual(runtime.main("renew", ["--config", "/tmp/evil"]), 2)
        load.assert_not_called()

    def test_entrypoint_rejects_unprivileged_coordinator(self):
        with patch.object(runtime.os, "geteuid", return_value=2000), patch.object(runtime, "load_policy") as load:
            self.assertEqual(runtime.main("renew", []), 1)
        load.assert_not_called()

    def test_observer_never_issues_or_reloads(self):
        with patch.object(runtime, "inspect_host"), patch.object(runtime, "active_fingerprint", return_value="abc"), \
                patch.object(runtime, "verify_endpoints") as verify, patch.object(runtime, "run_fixed") as run, \
                patch.object(runtime, "publication_locked", return_value=nullcontext(SimpleNamespace(a=Mock()))):
            runtime.observe(self.policy)
        verify.assert_called_once_with(self.policy, "abc")
        run.assert_not_called()

    def test_verify_main_does_not_create_status(self):
        with tempfile.TemporaryDirectory(prefix="tls-verify-status-") as name:
            private = Path(name)
            status_path = private / "status.json"
            with patch.object(runtime, "PRIVATE", private), patch.object(
                runtime, "STATUS", status_path
            ), patch.object(runtime.os, "geteuid", return_value=0), patch.object(
                runtime, "load_policy", return_value=self.policy
            ), patch.object(runtime, "locked", return_value=nullcontext()), patch.object(
                runtime, "observe"
            ):
                self.assertEqual(0, runtime.main("verify", []))
            self.assertFalse(status_path.exists())

    def test_publisher_callbacks_use_historical_and_fixed_adapter_boundaries(self):
        with tempfile.TemporaryDirectory(prefix="tls-runtime-publisher-") as name:
            base = Path(name)
            published = base / "published"
            private = base / "private"
            published.mkdir(mode=0o750)
            private.mkdir(mode=0o700)
            generation = published / ("generation-" + "1" * 32)
            generation.mkdir(mode=0o750)
            for filename in ("fullchain.pem", "privkey.pem"):
                (generation / filename).write_bytes(b"fixture")
            external = private / "candidate"
            external.mkdir()
            for filename in ("fullchain.pem", "privkey.pem"):
                (external / filename).write_bytes(b"fixture")
            commands = []
            with patch.object(runtime, "PUBLISHED", published), patch.object(
                runtime, "JOURNAL", private / "transaction.json"
            ), patch.object(
                runtime, "read_published_directory", return_value=(b"fixture", b"fixture")
            ), patch.object(runtime, "read_bounded", return_value=b"fixture"), patch.object(
                runtime, "validate_historical_certificate", return_value="1" * 64
            ) as historical, patch.object(
                runtime, "validate_certificate", return_value="2" * 64
            ) as current, patch.object(runtime, "secure_path"), patch.object(
                runtime, "run_fixed", side_effect=lambda argv, **_kwargs: commands.append(argv)
            ), patch.object(runtime, "verify_endpoints") as verify:
                deployment = SimpleNamespace(prepare=lambda record: None, locked=nullcontext,
                    operation=lambda action, path=None: commands.append(
                        [str(runtime.ADAPTER), action] + ([] if path is None else [str(path)])))
                publisher = runtime.publisher_for(self.policy, deployment)
                self.assertEqual("1" * 64, publisher.validate(generation))
                self.assertEqual("2" * 64, publisher.validate(external))
                publisher.preflight(generation)
                publisher.reload_and_verify("3" * 64)
                publisher.reload_and_verify("")

            historical.assert_called_once()
            self.assertEqual(2, current.call_count)
            self.assertEqual([
                [str(runtime.ADAPTER), "validate", str(generation)],
                [str(runtime.ADAPTER), "reload"],
                [str(runtime.ADAPTER), "deactivate"],
            ], commands)
            verify.assert_called_once_with(self.policy, "3" * 64)

    def test_issue_uses_fixed_credential_file_without_secret_argv(self):
        account = SimpleNamespace(pw_uid=2010, pw_gid=2010)
        with patch.object(runtime.pwd, "getpwnam", return_value=account), patch.object(
            runtime.os, "geteuid", return_value=2010
        ), patch.object(runtime, "secure_path"), patch.object(
            runtime, "validate_runtime_credential"
        ) as credential, patch.object(runtime, "run_issuer") as run:
            runtime.issue(self.policy)

        credential.assert_called_once_with(Path(runtime.TOKEN_RUNTIME), 2010, 2010)
        argv = run.call_args.args[0]
        environment = run.call_args.kwargs["environment"]
        self.assertNotIn("credential-value", argv)
        self.assertEqual(runtime.TOKEN_RUNTIME,
                         environment["CF_DNS_API_TOKEN_FILE"])
        self.assertNotIn("CF_DNS_API_TOKEN", environment)

    def test_start_issuer_uses_only_fixed_start_command_and_timeout(self):
        with patch.object(runtime, "run_fixed") as run:
            runtime.start_issuer()

        run.assert_called_once_with(
            ["/usr/bin/systemctl", "start", runtime.ISSUER_UNIT], timeout=960
        )

    def test_start_issuer_timeout_stops_the_fixed_unit(self):
        failure = runtime.subprocess.TimeoutExpired("systemctl", 960)
        with patch.object(runtime, "run_fixed", side_effect=[failure, None]) as run:
            with self.assertRaises(runtime.subprocess.TimeoutExpired):
                runtime.start_issuer()

        self.assertEqual([
            ((["/usr/bin/systemctl", "start", runtime.ISSUER_UNIT],), {"timeout": 960}),
            ((["/usr/bin/systemctl", "stop", runtime.ISSUER_UNIT],), {"timeout": 60}),
        ], [(call.args, call.kwargs) for call in run.call_args_list])

    def test_snapshot_uses_fixed_status_query_and_bounded_paths(self):
        account = SimpleNamespace(pw_uid=os.getuid())
        reads = [b"chain", b"key"]
        with patch.object(runtime.pwd, "getpwnam", return_value=account), patch.object(
            runtime, "secure_path"
        ), patch.object(
            runtime, "run_fixed", return_value=b"inactive\n"
        ) as run, patch.object(runtime, "read_bounded", side_effect=reads) as read:
            self.assertEqual((b"chain", b"key"), runtime.snapshot())

        run.assert_called_once_with(
            ["/usr/bin/systemctl", "show", runtime.ISSUER_UNIT,
             "--property=ActiveState", "--value"],
            timeout=60, capture=True,
        )
        self.assertEqual([
            ((runtime.ISSUER_STATE, "certificates/caddy.crt", os.getuid(), 262144), {}),
            ((runtime.ISSUER_STATE, "certificates/caddy.key", os.getuid(), 32768), {}),
        ], [(call.args, call.kwargs) for call in read.call_args_list])

    def test_snapshot_refuses_a_still_active_issuer_before_reading(self):
        account = SimpleNamespace(pw_uid=os.getuid())
        with patch.object(runtime.pwd, "getpwnam", return_value=account), patch.object(
            runtime, "secure_path"
        ), patch.object(
            runtime, "run_fixed", return_value=b"active\n"
        ) as run, patch.object(runtime, "read_bounded") as read:
            with self.assertRaisesRegex(RuntimeError, "issuer has not stopped"):
                runtime.snapshot()

        run.assert_called_once_with(
            ["/usr/bin/systemctl", "show", runtime.ISSUER_UNIT,
             "--property=ActiveState", "--value"],
            timeout=60, capture=True,
        )
        read.assert_not_called()

    def test_publication_roots_reject_inherited_default_acl(self):
        with tempfile.TemporaryDirectory(prefix="tls-publication-acl-test-") as name:
            root = Path(name)
            private = root / "private"
            published = root / "published"
            private.mkdir()
            published.mkdir()
            for target in (private, published):
                with self.subTest(target=target), _named_acl_on(
                    target, "system.posix_acl_default"
                ), self.assertRaises(ValueError):
                    runtime.validate_publication_acl_roots(private, published)

    def test_runtime_credential_accepts_readonly_service_owned_fallback(self):
        with tempfile.TemporaryDirectory(prefix="tls-credential-test-") as name:
            credential_root = Path(name)
            credential_root.chmod(0o755)
            unit_directory = credential_root / runtime.ISSUER_UNIT
            unit_directory.mkdir(mode=0o700)
            credential = unit_directory / "cloudflare-token"
            credential.write_bytes(b"never-read-by-test")
            credential.chmod(0o400)
            unit_directory.chmod(0o500)

            with patch.object(runtime, "_descriptor_read_only", return_value=True):
                runtime.validate_runtime_credential(
                    credential,
                    os.getuid(),
                    os.getgid(),
                    credential_root=credential_root,
                    trusted_uid=os.getuid(),
                    trusted_gid=os.getgid(),
                )

            credential.chmod(0o420)
            with patch.object(runtime, "_descriptor_read_only", return_value=True), \
                    self.assertRaises(ValueError):
                runtime.validate_runtime_credential(
                    credential, os.getuid(), os.getgid(),
                    credential_root=credential_root,
                    trusted_uid=os.getuid(), trusted_gid=os.getgid(),
                )

            credential.chmod(0o400)
            with patch.object(runtime, "_descriptor_read_only", return_value=False), \
                    self.assertRaises(ValueError):
                runtime.validate_runtime_credential(
                    credential, os.getuid(), os.getgid(),
                    credential_root=credential_root,
                    trusted_uid=os.getuid(), trusted_gid=os.getgid(),
                )

    def test_runtime_credential_accepts_readonly_v252_root_gid_fallback(self):
        with tempfile.TemporaryDirectory(prefix="tls-credential-v252-test-") as name:
            credential_root = Path(name)
            credential_root.chmod(0o755)
            unit_directory = credential_root / runtime.ISSUER_UNIT
            unit_directory.mkdir(mode=0o700)
            credential = unit_directory / "cloudflare-token"
            credential.write_bytes(b"never-read-by-test")
            credential.chmod(0o400)
            unit_directory.chmod(0o500)

            with patch.object(runtime, "_descriptor_read_only", return_value=True):
                runtime.validate_runtime_credential(
                    credential, os.getuid(), os.getgid() + 1000,
                    credential_root=credential_root,
                    trusted_uid=os.getuid(), trusted_gid=os.getgid(),
                )

    def test_runtime_credential_rejects_unrelated_fallback_gid(self):
        parent = SimpleNamespace(st_uid=2010, st_gid=3010)
        credential = SimpleNamespace(st_uid=2010, st_gid=3010)
        self.assertFalse(runtime._fallback_owners_match(
            parent, credential, service_uid=2010, service_gid=2010, trusted_gid=0,
        ))

    def test_runtime_credential_accepts_only_exact_service_acl(self):
        with tempfile.TemporaryDirectory(prefix="tls-credential-acl-test-") as name:
            credential_root = Path(name)
            credential_root.chmod(0o755)
            unit_directory = credential_root / runtime.ISSUER_UNIT
            unit_directory.mkdir(mode=0o700)
            credential = unit_directory / "cloudflare-token"
            credential.write_bytes(b"never-read-by-test")
            credential.chmod(0o440)
            unit_directory.chmod(0o550)
            service_uid = os.getuid() + 1000
            service_gid = os.getgid() + 1000

            def exact_acl(descriptor):
                permissions = 5 if stat.S_ISDIR(os.fstat(descriptor).st_mode) else 4
                return (
                    (runtime.ACL_USER_OBJ, permissions, runtime.ACL_UNDEFINED_ID),
                    (runtime.ACL_USER, permissions, service_uid),
                    (runtime.ACL_GROUP_OBJ, 0, runtime.ACL_UNDEFINED_ID),
                    (runtime.ACL_MASK, permissions, runtime.ACL_UNDEFINED_ID),
                    (runtime.ACL_OTHER, 0, runtime.ACL_UNDEFINED_ID),
                )

            with patch.object(runtime, "_read_posix_acl", side_effect=exact_acl):
                runtime.validate_runtime_credential(
                    credential, service_uid, service_gid,
                    credential_root=credential_root,
                    trusted_uid=os.getuid(), trusted_gid=os.getgid(),
                )

            def broad_acl(descriptor):
                values = list(exact_acl(descriptor))
                values.insert(-2, (runtime.ACL_GROUP, 4, service_gid + 1))
                return tuple(values)

            with patch.object(runtime, "_read_posix_acl", side_effect=broad_acl), \
                    self.assertRaises(ValueError):
                runtime.validate_runtime_credential(
                    credential, service_uid, service_gid,
                    credential_root=credential_root,
                    trusted_uid=os.getuid(), trusted_gid=os.getgid(),
                )

    def test_runtime_credential_rejects_symlink_without_following(self):
        with tempfile.TemporaryDirectory(prefix="tls-credential-link-test-") as name:
            credential_root = Path(name)
            credential_root.chmod(0o755)
            unit_directory = credential_root / runtime.ISSUER_UNIT
            unit_directory.mkdir(mode=0o500)
            source = credential_root / "source"
            source.write_bytes(b"source")
            credential = unit_directory / "cloudflare-token"
            unit_directory.chmod(0o700)
            credential.symlink_to(source)
            unit_directory.chmod(0o500)
            with self.assertRaises((ValueError, OSError)):
                runtime.validate_runtime_credential(
                    credential, os.getuid(), os.getgid(),
                    credential_root=credential_root,
                    trusted_uid=os.getuid(),
                    trusted_gid=os.getgid(),
                )

    def test_effective_unit_contract_rejects_arguments_and_boundary_drift(self):
        contract = runtime.UNIT_CONTRACTS[runtime.ISSUER_UNIT]
        lines = [
            "User=svc-acme", "Group=svc-acme", "SupplementaryGroups=",
            "Type=oneshot", "NoNewPrivileges=yes", "ProtectSystem=strict",
            "PrivateTmp=yes", "ReadWritePaths=/var/lib/homelab-tls-issuer",
            "LoadCredential=cloudflare-token:/etc/homelab-tls/cloudflare-token",
            "FragmentPath=/etc/systemd/system/homelab-tls-issuer.service",
            "DropInPaths=",
            "ExecCondition=", "ExecStartPre=", "ExecStartPost=",
            "ExecStart={ path=/usr/local/libexec/homelab-tls-issue ; argv[]=/usr/local/libexec/homelab-tls-issue ; ignore_errors=no ; start_time=[Mon 2026-09-07 01:02:03 MDT] ; stop_time=[Mon 2026-09-07 01:02:04 MDT] ; pid=742 ; code=exited ; status=0 }",
        ]

        typed = b'a(sasbttttuii) 0\na(sasbttttuii) 0\na(sasbttttuii) 0\na(ss) 1 "cloudflare-token" "/etc/homelab-tls/cloudflare-token"\n'
        fragment = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_gid=0)
        with patch.object(runtime, "run_fixed", side_effect=[("\n".join(lines) + "\n").encode(), typed]) as run, \
                patch.object(runtime, "secure_path", return_value=fragment):
            runtime.inspect_unit(runtime.ISSUER_UNIT, contract)
        self.assertIn("--all", run.call_args_list[0].args[0])

        mutations = {
            "User=svc-acme": "User=root",
            "Group=svc-acme": "Group=root",
            "SupplementaryGroups=": "SupplementaryGroups=wheel",
            "ReadWritePaths=/var/lib/homelab-tls-issuer": "ReadWritePaths=/",
            "LoadCredential=cloudflare-token:/etc/homelab-tls/cloudflare-token": "LoadCredential=",
            "argv[]=/usr/local/libexec/homelab-tls-issue": "argv[]=/usr/local/libexec/homelab-tls-issue --force",
            "ignore_errors=no": "ignore_errors=yes",
            "ExecCondition=": "ExecCondition={ path=/usr/bin/true ; argv[]=/usr/bin/true ; ignore_errors=no ; }",
            "ExecStartPre=": "ExecStartPre={ path=/usr/bin/true ; argv[]=/usr/bin/true ; ignore_errors=no ; }",
            "ExecStartPost=": "ExecStartPost={ path=/usr/bin/true ; argv[]=/usr/bin/true ; ignore_errors=no ; }",
        }
        for original, replacement in mutations.items():
            with self.subTest(replacement=replacement):
                changed = "\n".join(lines).replace(original, replacement).encode()
                with patch.object(runtime, "run_fixed", side_effect=[changed, typed]), patch.object(
                    runtime, "secure_path", return_value=fragment
                ), self.assertRaises(ValueError):
                    runtime.inspect_unit(runtime.ISSUER_UNIT, contract)

    def test_omitted_exec_arrays_require_explicit_typed_empty_evidence(self):
        contract = runtime.UNIT_CONTRACTS[runtime.ISSUER_UNIT]
        actual = dict(contract)
        for field in ("ExecCondition", "ExecStartPre", "ExecStartPost", "LoadCredential"):
            del actual[field]
        command = actual["ExecStart"]
        actual["ExecStart"] = "{ path=" + command + " ; argv[]=" + command + " ; ignore_errors=no }"
        show = ("\n".join(key + "=" + value for key, value in actual.items()) + "\n").encode()
        typed = b'a(sasbttttuii) 0\na(sasbttttuii) 0\na(sasbttttuii) 0\na(ss) 1 "cloudflare-token" "/etc/homelab-tls/cloudflare-token"\n'
        fragment = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_gid=0)
        with patch.object(runtime, "run_fixed", side_effect=[show, typed]) as run, patch.object(
                runtime, "secure_path", return_value=fragment):
            runtime.inspect_unit(runtime.ISSUER_UNIT, contract)
        self.assertEqual(["/usr/bin/busctl", "--system", "--timeout=15s", "get-property",
                          "org.freedesktop.systemd1",
                          "/org/freedesktop/systemd1/unit/homelab_2dtls_2dissuer_2eservice",
                          "org.freedesktop.systemd1.Service", "ExecCondition", "ExecStartPre", "ExecStartPost", "LoadCredential"],
                         run.call_args_list[1].args[0])
        for bad in (b"", b"a(sasbttttuii) 0\n", typed + typed,
                    typed.replace(b" 0", b" 1", 1), typed.replace(b"a(sasbttttuii)", b"as", 1)):
            with self.subTest(bad=bad), patch.object(runtime, "run_fixed", side_effect=[show, bad]), \
                    patch.object(runtime, "secure_path", return_value=fragment), self.assertRaises(ValueError):
                runtime.inspect_unit(runtime.ISSUER_UNIT, contract)
        with patch.object(runtime, "run_fixed", side_effect=[show, RuntimeError("query failed")]), \
                patch.object(runtime, "secure_path", return_value=fragment), self.assertRaises(RuntimeError):
            runtime.inspect_unit(runtime.ISSUER_UNIT, contract)


class PublishedGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tls-published-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "published"
        self.root.mkdir(mode=0o750)
        self.root.chmod(0o750)
        self.generation = self.root / ("generation-" + "1" * 32)
        self.generation.mkdir(mode=0o750)
        self.generation.chmod(0o750)
        for name, value in (("fullchain.pem", b"chain"), ("privkey.pem", b"key")):
            path = self.generation / name
            path.write_bytes(value)
            path.chmod(0o640)
        (self.root / "current").symlink_to(self.generation.name)
        self.uid = os.getuid()
        self.gid = os.getgid()

    def read(self, reader_gid=None):
        return runtime.read_published_generation(
            self.root, self.uid, self.gid if reader_gid is None else reader_gid
        )

    def test_exact_published_generation_is_read(self):
        self.assertEqual((b"chain", b"key"), self.read())

    def test_rejects_wrong_group_and_broad_modes(self):
        with self.assertRaises(ValueError):
            self.read(self.gid + 1)
        self.generation.chmod(0o755)
        with self.assertRaises(ValueError):
            self.read()
        self.generation.chmod(0o750)
        (self.generation / "privkey.pem").chmod(0o644)
        with self.assertRaises(ValueError):
            self.read()

    def test_rejects_hardlinks_symlinks_and_extra_entries(self):
        outside = self.root.parent / "outside"
        os.link(self.generation / "privkey.pem", outside)
        with self.assertRaises(ValueError):
            self.read()
        outside.unlink()
        key = self.generation / "privkey.pem"
        key.unlink()
        key.symlink_to(self.generation / "fullchain.pem")
        with self.assertRaises(ValueError):
            self.read()
        key.unlink()
        key.write_bytes(b"key")
        key.chmod(0o640)
        (self.generation / "unexpected").write_bytes(b"bad")
        with self.assertRaises(ValueError):
            self.read()

    def test_rejects_named_acl_reader_on_each_published_object(self):
        targets = [
            self.root,
            self.generation,
            self.generation / "fullchain.pem",
            self.generation / "privkey.pem",
        ]
        for target in targets:
            with self.subTest(target=target), _named_acl_on(target), \
                    self.assertRaises(ValueError):
                self.read()


class TLSTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Temporary TLS test root")])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
              .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False).sign(key, hashes.SHA256()))
        leafkey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unused")]))
                .issuer_name(name).public_key(leafkey.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=2))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("*.infra.example.com")]), False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False)
                .sign(key, hashes.SHA256()))
        pem = serialization.Encoding.PEM
        (root / "ca.pem").write_bytes(ca.public_bytes(pem))
        (root / "server.pem").write_bytes(leaf.public_bytes(pem))
        (root / "key.pem").write_bytes(leafkey.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.fingerprint = hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest()
        self.context = ssl.create_default_context(cafile=str(root / "ca.pem"))
        self.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server_context.load_cert_chain(root / "server.pem", root / "key.pem")

    def handshake(self, hostname, fingerprint, context):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve():
            conn, _ = listener.accept()
            try:
                with self.server_context.wrap_socket(conn, server_side=True):
                    pass
            except ssl.SSLError:
                conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 6)
        runtime.verify_endpoint({"hostname": hostname, "address": "127.0.0.1", "port": listener.getsockname()[1]},
                                fingerprint, context)

    def test_fresh_trusted_tls_fingerprint(self):
        self.handshake("modem.infra.example.com", self.fingerprint, self.context)

    def test_direct_unifi_hostname_is_not_authenticated(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.handshake("udm.example.com", self.fingerprint, self.context)

    def test_unknown_root_rejected(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.handshake("modem.infra.example.com", self.fingerprint, ssl.create_default_context())

    def test_different_served_certificate_rejected(self):
        with self.assertRaises(ValueError):
            self.handshake("modem.infra.example.com", "0" * 64, self.context)


class AttemptStatusTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="tls-attempt-test-"))
        self.private = Path(temporary)
        self.status = self.private / "status.json"
        self.status.write_text('{"publication":"changed"}')
        self.status.chmod(0o600)
        for name, value in (("PRIVATE", self.private), ("STATUS", self.status)):
            self.stack.enter_context(patch.object(runtime, name, value))
        self.stack.enter_context(patch.object(runtime.os, "geteuid", return_value=0))
        self.stack.enter_context(patch.object(runtime, "load_policy", return_value=parse_policy(json.dumps(example_policy()).encode())))
        self.stack.enter_context(patch.object(runtime, "locked", return_value=nullcontext()))
        # Host ownership cannot be created by the unprivileged controller test.
        self.stack.enter_context(patch.object(runtime, "validate_status_directory", create=True))

    def test_preflight_failure_replaces_previous_success_without_exception_detail(self):
        with patch.object(runtime, "inspect_host", side_effect=ValueError("private detail")):
            self.assertEqual(1, runtime.main("renew", []))
        status = json.loads(self.status.read_text())
        self.assertEqual("failed", status.get("preflight"))
        self.assertEqual("not_run", status["publication"])
        self.assertEqual("not_run", status["issuance"])
        self.assertNotIn("private detail", self.status.read_text())

    def test_interruption_leaves_in_progress_instead_of_previous_success(self):
        with patch.object(runtime, "inspect_host", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            runtime.main("renew", [])
        status = json.loads(self.status.read_text())
        self.assertEqual("in_progress", status.get("attempt"))
        self.assertEqual("not_run", status["publication"])

    def test_busy_lock_never_overwrites_holder_status(self):
        before = self.status.read_bytes()
        with patch.object(runtime, "locked", side_effect=RuntimeError("busy")):
            self.assertEqual(1, runtime.main("renew", []))
        self.assertEqual(before, self.status.read_bytes())

    def test_observer_never_writes_status(self):
        before = self.status.read_bytes()
        with patch.object(runtime, "observe"):
            self.assertEqual(0, runtime.main("verify", []))
        self.assertEqual(before, self.status.read_bytes())

    def test_unsafe_status_boundary_does_not_replace_prior_status(self):
        before = self.status.read_bytes()
        with patch.object(runtime, "validate_status_directory", side_effect=ValueError("unsafe")):
            self.assertEqual(1, runtime.main("renew", []))
        self.assertEqual(before, self.status.read_bytes())


class StatusDirectoryBoundaryTests(unittest.TestCase):
    def test_status_directory_requires_exact_root_private_mode_and_no_acl(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_gid=0)
        with patch.object(runtime, "secure_path"), patch.object(
                runtime, "_directory_without_extra_acl", return_value=metadata):
            runtime.validate_status_directory()
            for field, unsafe in (("st_uid", 1000), ("st_gid", 1000), ("st_mode", stat.S_IFDIR | 0o750)):
                original = getattr(metadata, field)
                setattr(metadata, field, unsafe)
                with self.subTest(field=field), self.assertRaises(ValueError):
                    runtime.validate_status_directory()
                setattr(metadata, field, original)
        with patch.object(runtime, "secure_path"), patch.object(
                runtime, "_directory_without_extra_acl", side_effect=ValueError("named ACL")), self.assertRaises(ValueError):
            runtime.validate_status_directory()


class IssuerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.log = self.directory / "last-issue.log"
        state = patch.object(runtime, "ISSUER_STATE", self.directory)
        state.start()
        self.addCleanup(state.stop)

    def run_script(self, script, timeout=5):
        self.assertTrue(callable(getattr(runtime, "run_issuer", None)),
                        "issuer output must have a protected diagnostic runner")
        return runtime.run_issuer([sys.executable, "-c", script],
                                  environment=runtime.CLEAN_ENV, timeout=timeout)

    def test_failed_child_keeps_both_streams_private_and_preserves_failure(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaisesRegex(RuntimeError, "issuer exited with code 7") as caught:
                self.run_script("import sys; print('synthetic stdout'); "
                                "print('synthetic private error', file=sys.stderr); sys.exit(7)")
        self.assertEqual("", output.getvalue() + errors.getvalue())
        self.assertNotIn("synthetic", str(caught.exception))
        self.assertIn(b"synthetic stdout", self.log.read_bytes())
        self.assertIn(b"synthetic private error", self.log.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(self.log.stat().st_mode))
        self.assertEqual(os.geteuid(), self.log.stat().st_uid)

    def test_only_latest_attempt_and_last_64_kib_are_retained(self):
        self.run_script("print('previous attempt')")
        self.run_script("import os; os.write(1, b'x' * 200000 + b'END')")
        self.assertEqual(b"x" * (65536 - 3) + b"END", self.log.read_bytes())
        self.assertEqual([self.log], list(self.directory.iterdir()))

    def test_timeout_keeps_partial_output_and_stops_child(self):
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "issuer timed out"):
            self.run_script("import os,time; print('started', os.getpid(), flush=True); "
                            "time.sleep(30)", timeout=0.5)
        self.assertLess(time.monotonic() - started, 5)
        words = self.log.read_text().split()
        self.assertEqual("started", words[0])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(words[1]), 0)

    def test_closed_output_does_not_bypass_timeout(self):
        with self.assertRaisesRegex(RuntimeError, "issuer timed out"):
            self.run_script("import os,time; os.close(1); os.close(2); time.sleep(30)", timeout=0.5)

    def test_timeout_drains_output_already_buffered_before_deadline(self):
        ready = self.directory / "ready"
        clock = time.monotonic

        def expired_after_child_writes():
            deadline = clock() + 5
            while not ready.exists():
                if clock() >= deadline:
                    self.fail("fixture did not write its diagnostic")
                time.sleep(0.01)
            return 100

        clock_first = [True]

        def advance_clock():
            if clock_first[0]:
                clock_first[0] = False
                return 0
            return expired_after_child_writes()

        with patch.object(runtime.time, "monotonic", side_effect=advance_clock):
            with self.assertRaisesRegex(RuntimeError, "issuer timed out"):
                self.run_script("import os,time; from pathlib import Path; "
                                "os.write(2, b'final buffered diagnostic'); "
                                f"Path({str(ready)!r}).touch(); time.sleep(30)", timeout=1)
        self.assertEqual(b"final buffered diagnostic", self.log.read_bytes())

    def test_log_symlink_is_replaced_without_touching_target(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "keep"
            target.write_text("keep")
            self.log.symlink_to(target)
            self.run_script("print('new diagnostic')")
            self.assertEqual("keep", target.read_text())
            self.assertFalse(self.log.is_symlink())
            self.assertEqual(b"new diagnostic\n", self.log.read_bytes())

    def test_public_directory_is_rejected_before_child_runs(self):
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private issuer diagnostic directory"):
            self.run_script("raise SystemExit(0)")
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
