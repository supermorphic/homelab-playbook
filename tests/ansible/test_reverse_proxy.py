"""Independent public input contract for the shared HTTPS proxy."""

import copy
import importlib.util
from pathlib import Path
import unittest

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]


class ReverseProxyInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "roles/reverse_proxy/filter_plugins/proxy.py"
        spec = importlib.util.spec_from_file_location("reverse_proxy_filter", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def fixture(self):
        return {"bind_addresses": ["10.0.0.2"], "client_sources": ["10.0.0.0/24"],
                "routes": [{"hostname": "app.example.test", "backend_port": 8080,
                            "certificate_name": "app"}]}

    def device_route(self, transport="https"):
        backend = {"transport": transport, "address": "10.0.0.50",
                   "port": 443 if transport == "https" else 80}
        if transport == "https":
            backend.update({"server_name": "DEVICE.Example.test", "trust_name": "device"})
        return {"hostname": "device.example.test", "certificate_name": "infra",
                "backend": backend}

    def test_empty_routes_need_no_ingress(self):
        self.assertEqual(self.module.validate({"bind_addresses": [], "client_sources": [], "routes": []}),
                         {"bind_addresses": [], "client_sources": [], "routes": []})

    def test_health_route_without_a_backend(self):
        value = self.fixture()
        route = {"hostname": "caddy.example.test", "certificate_name": "app", "health": True}
        value["routes"].append(route)
        self.assertEqual(self.module.validate(value)["routes"][1], route)
        rendered = self.module.render(value)
        health = rendered.split("https://caddy.example.test:443 {", 1)[1]
        self.assertNotIn("reverse_proxy", health)
        self.assertIn("/etc/caddy/tls/app/current/fullchain.pem", health)
        self.assertIn("reverse_proxy 127.0.0.1:8080", rendered)

    def test_health_route_rejects_ambiguous_or_custom_responses(self):
        route = {"hostname": "caddy.example.test", "certificate_name": "app", "health": True}
        for change in ({"health": False}, {"health": 1}, {"health": "true"},
                       {"health": {}}, {"backend_port": 8080},
                       {"backend": self.device_route()["backend"]},
                       {"path": "/custom"}, {"body": "details"}):
            value = self.fixture()
            value["routes"] = [dict(route, **change)]
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.module.validate(value)

    def test_health_route_requires_private_ingress_and_unique_hostname(self):
        for field in ("bind_addresses", "client_sources"):
            value = self.fixture()
            value["routes"] = [{"hostname": "caddy.example.test", "certificate_name": "app",
                                "health": True}]
            value[field] = []
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.module.validate(value)
        value = self.fixture()
        value["routes"].append({"hostname": "APP.example.test", "certificate_name": "app",
                                "health": True})
        with self.assertRaises(ValueError):
            self.module.validate(value)

    def test_canonical_values_without_mutating_input(self):
        value = self.fixture()
        value["bind_addresses"] += ["fd00:0:0::2"]
        value["client_sources"] += ["fd00::/64"]
        value["routes"][0]["hostname"] = "APP.Example.test"
        original = copy.deepcopy(value)
        result = self.module.validate(value)
        self.assertEqual(result["bind_addresses"], ["10.0.0.2", "fd00::2"])
        self.assertEqual(result["routes"][0]["hostname"], "app.example.test")
        self.assertEqual(value, original)

    def test_route_cannot_inject_config_or_path(self):
        for field, values in {"hostname": ["app.example.test\nadmin :2019", "*.example.test", "a/b", "localhost", "a..test", "-a.test", "a.test:443", "https://a.test", "10.0.0.2"],
                              "certificate_name": ["../app", ".", "a/b", "app\ntls internal", "app key", "-app"]}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    data = self.fixture()
                    data["routes"][0][field] = value
                    with self.assertRaises(ValueError):
                        self.module.validate(data)

    def test_ports_are_unprivileged_integers(self):
        for value in (True, False, 443, 1023, 65536, "8080", 8080.0, None):
            data = self.fixture()
            data["routes"][0]["backend_port"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.module.validate(data)
        for value in (1024, 65535):
            data = self.fixture()
            data["routes"][0]["backend_port"] = value
            self.assertEqual(self.module.validate(data)["routes"][0]["backend_port"], value)

    def test_tagged_ansible_integer_is_accepted_without_accepting_booleans(self):
        class TaggedInteger(int):
            pass

        data = self.fixture()
        data["routes"][0]["backend_port"] = TaggedInteger(8080)
        self.assertEqual(self.module.validate(data)["routes"][0]["backend_port"], 8080)

    def test_duplicate_hostname_is_case_insensitive(self):
        data = self.fixture()
        data["routes"].append(dict(data["routes"][0], hostname="APP.EXAMPLE.TEST"))
        with self.assertRaises(ValueError):
            self.module.validate(data)

    def test_only_explicit_private_addresses_and_networks(self):
        for field, values in {"bind_addresses": ["0.0.0.0", "::", "127.0.0.1", "169.254.1.1", "192.0.2.2", "8.8.8.8", "fe80::1", "fd00::1%eth0", "10.0.0.2/24"],
                              "client_sources": ["0.0.0.0/0", "::/0", "127.0.0.0/8", "192.0.2.0/24", "10.0.0.1/24", "10.0.0.2", "fe80::/64"]}.items():
            for value in values:
                data = self.fixture()
                data[field] = [value]
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.module.validate(data)

    def test_schema_rejects_unknown_fields_and_wrong_types(self):
        for change in ({"extra": True}, {"routes": {}}, {"routes": [None]},
                       {"bind_addresses": "10.0.0.2"}, {"client_sources": []},
                       {"bind_addresses": []}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.module.validate(dict(self.fixture(), **change))
        data = self.fixture()
        data["routes"][0]["upstream"] = "remote:8080"
        with self.assertRaises(ValueError):
            self.module.validate(data)

    def test_device_routes_are_canonical_without_mutating_input(self):
        data = self.fixture()
        data["routes"] = [self.device_route(), self.device_route("http")]
        data["routes"][1]["hostname"] = "sensor.example.test"
        data["routes"][1]["backend"]["address"] = "fd00:0:0::60"
        original = copy.deepcopy(data)

        result = self.module.validate(data)

        self.assertEqual(
            result["routes"],
            [
                {
                    "hostname": "device.example.test",
                    "certificate_name": "infra",
                    "backend": {
                        "transport": "https",
                        "address": "10.0.0.50",
                        "port": 443,
                        "server_name": "device.example.test",
                        "trust_name": "device",
                    },
                },
                {
                    "hostname": "sensor.example.test",
                    "certificate_name": "infra",
                    "backend": {
                        "transport": "http",
                        "address": "fd00::60",
                        "port": 80,
                    },
                },
            ],
        )
        self.assertEqual(data, original)

    def test_device_route_rejects_ambiguous_or_unsafe_fields(self):
        changes = (
            {"backend_port": 8080},
            {"upstream": "https://10.0.0.50:443"},
            {"tls_insecure_skip_verify": True},
        )
        for change in changes:
            data = self.fixture()
            data["routes"] = [dict(self.device_route(), **change)]
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.module.validate(data)

        for field, values in {
            "transport": ["h2c", "HTTP", "", None],
            "address": ["device.example.test", "127.0.0.1", "169.254.1.1",
                        "192.0.2.50", "10.0.0.50/32", "fd00::1%eth0"],
            "port": [True, 0, 65536, "443", 443.0, None],
        }.items():
            for value in values:
                data = self.fixture()
                route = self.device_route()
                route["backend"][field] = value
                data["routes"] = [route]
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.module.validate(data)

    def test_http_device_rejects_tls_fields_and_https_requires_them(self):
        for field, value in {
            "server_name": "device.example.test",
            "trust_name": "device",
            "tls_insecure_skip_verify": True,
            "trust_path": "/tmp/device.pem",
        }.items():
            data = self.fixture()
            route = self.device_route("http")
            route["backend"][field] = value
            data["routes"] = [route]
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.module.validate(data)

        for missing in ("server_name", "trust_name"):
            data = self.fixture()
            route = self.device_route()
            del route["backend"][missing]
            data["routes"] = [route]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                self.module.validate(data)

    def test_https_device_rejects_unsafe_server_and_trust_names(self):
        for field, values in {
            "server_name": ["*.example.test", "10.0.0.50", "localhost", "a/b.test",
                            "device.example.test\ntls_insecure_skip_verify"],
            "trust_name": ["../device", ".", "a/b", "device root", "-device",
                           "device\ntls_insecure_skip_verify"],
        }.items():
            for value in values:
                data = self.fixture()
                route = self.device_route()
                route["backend"][field] = value
                data["routes"] = [route]
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.module.validate(data)

    def test_deferred_certificate_marker_is_narrow_and_optional(self):
        self.assertNotIn("deferred_certificates", self.module.validate(self.fixture()))
        for deferred in ([], ["infra"]):
            data = dict(self.fixture(), deferred_certificates=deferred)
            self.assertEqual(self.module.validate(data)["deferred_certificates"], deferred)
        for deferred in (["app"], ["infra", "infra"], "infra", None, {}):
            data = dict(self.fixture(), deferred_certificates=deferred)
            with self.subTest(deferred=deferred), self.assertRaises(ValueError):
                self.module.validate(data)

    def test_render_is_deterministic_for_local_http_and_https_routes(self):
        data = self.fixture()
        data["bind_addresses"].append("fd00::2")
        data["routes"] = [self.device_route("http"), self.fixture()["routes"][0],
                          self.device_route()]
        data["routes"][0]["hostname"] = "sensor.example.test"
        data["routes"][0]["backend"]["address"] = "fd00::60"

        rendered = self.module.render(self.module.validate(data))

        self.assertEqual(rendered, """\
{
\tadmin unix//run/caddy/admin.sock
\tauto_https off
\tservers {
\t\tprotocols h1 h2
\t}
}

https://app.example.test:443 {
\tbind 10.0.0.2 fd00::2
\ttls /etc/caddy/tls/app/current/fullchain.pem /etc/caddy/tls/app/current/privkey.pem
\treverse_proxy 127.0.0.1:8080
}

https://device.example.test:443 {
\tbind 10.0.0.2 fd00::2
\ttls /etc/caddy/tls/infra/current/fullchain.pem /etc/caddy/tls/infra/current/privkey.pem
\treverse_proxy https://10.0.0.50:443 {
\t\ttransport http {
\t\t\ttls_trusted_ca_certs /etc/caddy/trust/device.pem
\t\t\ttls_server_name device.example.test
\t\t}
\t}
}

https://sensor.example.test:443 {
\tbind 10.0.0.2 fd00::2
\ttls /etc/caddy/tls/infra/current/fullchain.pem /etc/caddy/tls/infra/current/privkey.pem
\treverse_proxy http://[fd00::60]:80
}
""")
        self.assertNotIn("tls_insecure_skip_verify", rendered)

    def test_render_uses_only_safe_internal_certificate_overrides(self):
        data = self.fixture()
        self.assertIn(
            "tls /etc/caddy/tls/.homelab-tls-private/staging/generation-1/fullchain.pem "
            "/etc/caddy/tls/.homelab-tls-private/staging/generation-1/privkey.pem",
            self.module.render(
                self.module.validate(data),
                {"app": "/etc/caddy/tls/.homelab-tls-private/staging/generation-1"},
            ),
        )
        for overrides in (
            {"other": "/etc/caddy/tls/other/current"},
            {"app": "relative/generation"},
            {"app": "/etc/caddy/tls/app/../other"},
            {"app": "/etc/caddy/tls/app/current\ntls internal"},
            [("app", "/etc/caddy/tls/app/current")],
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.module.render(self.module.validate(data), overrides)

        data["certificate_paths"] = {"app": "/tmp/generation"}
        with self.assertRaises(ValueError):
            self.module.validate(data)

    def test_ansible_template_uses_the_shared_renderer(self):
        environment = Environment(
            loader=FileSystemLoader(ROOT / "roles/reverse_proxy/templates"),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        filters = self.module.FilterModule().filters()
        self.assertIs(filters["reverse_proxy_render"], self.module.render)
        environment.filters.update(filters)
        config = self.module.validate(self.fixture())

        rendered = environment.get_template("Caddyfile.j2").render(
            reverse_proxy_config=config
        )

        self.assertEqual(rendered, self.module.render(config))


if __name__ == "__main__":
    unittest.main()
