"""核心逻辑单元测试：不启动 GUI、不访问网络、不改动真实配置。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hy2_config as C  # noqa: E402
import hy2_downloader as D  # noqa: E402


def good(**overrides):
    values = C.default_values()
    values["_serverHost"] = "hy.example.net"
    values["acme.domains"] = [{"domain": "hy.example.net"}]
    values["acme.email"] = "admin@hy.example.net"
    values.update(overrides)
    return values


class SchemaTests(unittest.TestCase):
    def test_schema_loads(self):
        schema = C.load_schema()
        self.assertGreaterEqual(len(schema["tabs"]), 10)
        self.assertGreaterEqual(len(C.fields(schema)), 80)
        keys = [f["key"] for f in C.fields(schema)]
        self.assertEqual(len(keys), len(set(keys)), "字段 key 必须唯一")
        for f in C.fields(schema):
            self.assertIn("type", f)
            self.assertIn("default", f)
            self.assertIn(f["type"], ("str", "secret", "int", "bool", "choice", "text", "list"))

    def test_defaults_generate_password(self):
        a = C.default_values()
        b = C.default_values()
        self.assertTrue(a["auth.password"])
        self.assertNotEqual(a["auth.password"], b["auth.password"], "默认密码必须随机")


class BuildTests(unittest.TestCase):
    def test_default_build(self):
        cfg = C.build_config(good())
        self.assertEqual(cfg["listen"], ":443")
        self.assertEqual(cfg["auth"]["type"], "password")
        self.assertEqual(cfg["acme"]["type"], "http")
        self.assertIn("domains", cfg["acme"])
        self.assertNotIn("tls", cfg)
        self.assertNotIn("obfs", cfg)
        self.assertNotIn("resolver", cfg)
        self.assertNotIn("quic", cfg)
        self.assertEqual(cfg["masquerade"]["type"], "proxy")
        self.assertEqual(yaml.safe_load(C.dump_yaml(cfg)), cfg)

    def test_tls_mode_excludes_acme(self):
        values = good(_tlsMode="tls", **{"tls.cert": "C:/cert/fullchain.pem", "tls.key": "C:/cert/key.pem"})
        cfg = C.build_config(values)
        self.assertIn("tls", cfg)
        self.assertNotIn("acme", cfg)
        self.assertEqual(cfg["tls"]["cert"], "C:/cert/fullchain.pem")

    def test_userpass_transform(self):
        values = good(_authType="userpass", **{
            "auth.userpass": [{"user": "alice", "pass": "p1"}, {"user": "bob", "pass": "p2"}]
        })
        cfg = C.build_config(values)
        self.assertEqual(cfg["auth"]["type"], "userpass")
        self.assertEqual(cfg["auth"]["userpass"], {"alice": "p1", "bob": "p2"})
        self.assertNotIn("password", cfg["auth"])

    def test_obfs_gecko(self):
        values = good(_obfsType="gecko", **{"obfs.gecko.password": "sec", "obfs.gecko.minPacketSize": 512,
                                            "obfs.gecko.maxPacketSize": 1200})
        cfg = C.build_config(values)
        self.assertEqual(cfg["obfs"]["type"], "gecko")
        self.assertEqual(cfg["obfs"]["gecko"]["password"], "sec")
        self.assertNotIn("salamander", cfg["obfs"])

    def test_outbounds_extra_json(self):
        values = good(**{"outbounds": [
            {"name": "d", "type": "direct", "extra": '{"mode": "auto"}'},
            {"name": "s", "type": "socks5", "extra": '{"addr": "1.2.3.4:1080"}'},
        ]})
        cfg = C.build_config(values)
        self.assertEqual(cfg["outbounds"][1]["socks5"]["addr"], "1.2.3.4:1080")

    def test_advanced_yaml_deep_merge(self):
        values = good(_quicEnabled=True, _advancedYaml="quic:\n  maxIdleTimeout: 60s\nspeedTest: true\n")
        cfg = C.build_config(values)
        self.assertEqual(cfg["quic"]["maxIdleTimeout"], "60s")
        self.assertEqual(cfg["quic"]["maxIncomingStreams"], 1024)
        self.assertTrue(cfg["speedTest"])

    def test_resolver_and_sniff(self):
        values = good(_resolverType="tls", _sniffEnabled=True,
                      **{"resolver.tls.addr": "1.1.1.1:853", "sniff.tcpPorts": "80,443"})
        cfg = C.build_config(values)
        self.assertEqual(cfg["resolver"]["type"], "tls")
        self.assertNotIn("udp", cfg["resolver"])
        self.assertTrue(cfg["sniff"]["enable"])

    def test_acl_rules(self):
        raw = '- outbound: reject\n  match: geoip("cn")\n- outbound: direct\n'
        cfg = C.build_config(good(_aclMode="rules", **{"acl.rules": raw, "acl.geoip": "geoip.dat"}))
        self.assertEqual(len(cfg["acl"]["rules"]), 2)
        self.assertEqual(cfg["acl"]["geoip"], "geoip.dat")
        self.assertNotIn("inline", cfg["acl"])


class ValidateTests(unittest.TestCase):
    def test_good_config_passes(self):
        self.assertEqual(C.validate(good()), [])

    def test_windows_port_range_rejected(self):
        values = good(listen=":20000-50000")
        self.assertTrue(any("Windows 不支持端口范围" in e for e in C.validate(values)))
        self.assertTrue(any("Windows 不支持端口范围" in e for e in C.validate(good(listen=":443,5000-6000"))))

    def test_example_domain_rejected(self):
        values = good()
        values["acme.domains"] = [{"domain": "example.com"}]
        self.assertTrue(any("示例值" in e for e in C.validate(values)))

    def test_tls_and_acme_mutex(self):
        values = good(_tlsMode="tls")
        self.assertTrue(any("cert" in e for e in C.validate(values)))

    def test_gecko_bounds(self):
        values = good(_obfsType="gecko", **{"obfs.gecko.password": "x", "obfs.gecko.minPacketSize": 1500,
                                            "obfs.gecko.maxPacketSize": 900})
        self.assertTrue(any("Gecko 分片" in e for e in C.validate(values)))

    def test_bandwidth_format(self):
        values = good(_bandwidthEnabled=True, **{"bandwidth.up": "abc"})
        self.assertTrue(any("格式不正确" in e for e in C.validate(values)))

    def test_bad_advanced_yaml(self):
        values = good(_advancedYaml="- a\n- b\n")
        self.assertTrue(any("附加 YAML" in e for e in C.validate(values)))

    def test_warnings(self):
        values = good(_statsEnabled=True, _masqType="none")
        self.assertTrue(any("secret" in w for w in C.warnings(values)))


class ShareTests(unittest.TestCase):
    def test_uri_with_obfs(self):
        values = good(_obfsType="salamander", **{"auth.password": "p@ss word", "obfs.salamander.password": "obf"})
        uri = C.share_uri(values, "my node")
        self.assertTrue(uri.startswith("hysteria2://"))
        self.assertIn("p%40ss%20word", uri)
        self.assertIn("obfs=salamander", uri)
        self.assertIn("obfs-password=obf", uri)
        self.assertIn("#my%20node", uri)

    def test_userpass_uri(self):
        values = good(_authType="userpass", **{"auth.userpass": [{"user": "u", "pass": "p"}]})
        self.assertIn("u:p@", C.share_uri(values, "x"))


class RoundTripTests(unittest.TestCase):
    def test_values_from_yaml(self):
        values = good(_authType="userpass", _resolverType="udp", **{
            "auth.userpass": [{"user": "a", "pass": "b"}],
            "resolver.udp.addr": "8.8.4.4:53",
        })
        cfg = C.build_config(values)
        back = C.values_from_yaml(cfg)
        self.assertEqual(back["_authType"], "userpass")
        self.assertEqual(back["_tlsMode"], "acme")
        self.assertEqual(back["_resolverType"], "udp")
        self.assertEqual(back["auth.userpass"], [{"user": "a", "pass": "b"}])

    def test_prune_and_merge(self):
        self.assertEqual(C.prune({"a": {}, "b": [], "c": None, "d": False, "e": 0}), {"d": False, "e": 0})
        self.assertEqual(C.deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 3}}), {"a": {"x": 1, "y": 3}})

    def test_parse_kv_and_lines(self):
        self.assertEqual(C.parse_kv("content-type: text/plain\nx=1\n# comment\n"),
                         {"content-type": "text/plain", "x": "1"})
        self.assertEqual(C.parse_lines("a\n\n b \n#c\n"), ["a", "b"])


class DownloaderTests(unittest.TestCase):
    def test_parse_hashes(self):
        line = "6ee18e99f826670a249ad20400c536900cb6fb5369237e30768df9032ac92d80  build/hysteria-windows-amd64.exe\n"
        parsed = D.parse_hashes(line)
        self.assertEqual(parsed["hysteria-windows-amd64.exe"],
                         "6ee18e99f826670a249ad20400c536900cb6fb5369237e30768df9032ac92d80")

    def test_filename_for(self):
        self.assertEqual(D.filename_for("amd64"), "hysteria-windows-amd64.exe")
        self.assertEqual(D.filename_for("arm64"), "hysteria-windows-arm64.exe")
        with self.assertRaises(ValueError):
            D.filename_for("mips")


if __name__ == "__main__":
    unittest.main(verbosity=2)
