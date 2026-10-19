"""netutil：可信代理解析 X-Forwarded-For，以及 4 个 HTTP 取 IP 调用点的开关行为。

IP 一律用文档保留段（RFC 5737 / RFC 3849），不出现任何真实地址。
"""
from __future__ import annotations

import ipaddress
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.requests import Request

from backend.app import asr_corrections, diagnostics
from backend.app.netutil import (
    IpResolver,
    _norm,
    legacy_ip_key,
    parse_networks,
    real_client_ip,
    request_ip_override,
    xff_of,
)
from backend.app.ratelimit import RateLimitMiddleware

# 网关在本机，负载均衡在 10.0.0.0/16（私网），客户端在 198.51.100.0/24
GATEWAY_AND_LB, _ = parse_networks(["127.0.0.1/32", "::1/128", "10.0.0.0/16"])
GATEWAY_ONLY, _ = parse_networks(["127.0.0.1/32"])


class NormTests(unittest.TestCase):
    def test_parses_common_forms(self) -> None:
        cases = {
            "198.51.100.7": "198.51.100.7",
            " 198.51.100.7 ": "198.51.100.7",
            "198.51.100.7:4567": "198.51.100.7",
            "2001:db8::1": "2001:db8::1",
            "[2001:db8::1]": "2001:db8::1",
            "[2001:db8::1]:443": "2001:db8::1",
            "fe80::1%eth0": "fe80::1",
            # IPv4 映射地址归一成 IPv4
            "::ffff:198.51.100.7": "198.51.100.7",
            "[::ffff:198.51.100.7]:80": "198.51.100.7",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(str(_norm(raw)), expected)
        self.assertIsInstance(_norm("::ffff:198.51.100.7"), ipaddress.IPv4Address)

    def test_rejects_garbage(self) -> None:
        for raw in (None, "", "unknown", "198.51.100.7:", "198.51.100.7:http", "[2001:db8::1", "[2001:db8::1]x",
                    "300.1.1.1", "198.51.100", "a" * 100, "198.51.100.7:99999999"):
            with self.subTest(raw=raw):
                self.assertIsNone(_norm(raw))


class RealClientIpTests(unittest.TestCase):
    def test_matrix(self) -> None:
        cases = [
            # (说明, 对端, XFF, 可信网段, 期望)
            ("对端不可信：XFF 一概不看", "203.0.113.9", "198.51.100.1", GATEWAY_AND_LB, "203.0.113.9"),
            ("网关 127.0.0.1 → 负载均衡 → 客户端", "127.0.0.1", "198.51.100.7, 10.0.1.23", GATEWAY_AND_LB, "198.51.100.7"),
            ("http-proxy xfwd 拼接不带空格", "127.0.0.1", "198.51.100.7,10.0.1.23", GATEWAY_AND_LB, "198.51.100.7"),
            ("最左伪造无效", "127.0.0.1", "192.0.2.66, 198.51.100.7, 10.0.1.23", GATEWAY_AND_LB, "198.51.100.7"),
            ("漏配负载均衡网段：解析成负载均衡地址", "127.0.0.1", "198.51.100.7, 10.0.1.23", GATEWAY_ONLY, "10.0.1.23"),
            ("全部可信：取最左", "127.0.0.1", "10.0.0.5, 10.0.1.23", GATEWAY_AND_LB, "10.0.0.5"),
            ("右侧是垃圾：停在最后一个可信地址", "127.0.0.1", "198.51.100.7, unknown", GATEWAY_AND_LB, "127.0.0.1"),
            ("垃圾在客户端左边：不影响", "127.0.0.1", "garbage, 198.51.100.7, 10.0.1.23", GATEWAY_AND_LB, "198.51.100.7"),
            ("可信对端没有 XFF：对端", "127.0.0.1", None, GATEWAY_AND_LB, "127.0.0.1"),
            ("v6 对端 + v4 客户端", "::1", "198.51.100.7", GATEWAY_AND_LB, "198.51.100.7"),
            ("IPv4 映射的对端也算可信", "::ffff:127.0.0.1", "198.51.100.7", GATEWAY_AND_LB, "198.51.100.7"),
            ("IPv4 映射的 XFF 条目归一", "127.0.0.1", "::ffff:198.51.100.7", GATEWAY_AND_LB, "198.51.100.7"),
            ("条目带端口", "127.0.0.1", "198.51.100.7:4567, 10.0.1.23:80", GATEWAY_AND_LB, "198.51.100.7"),
            ("空格分隔也认（与 nginx 一致）", "127.0.0.1", "198.51.100.7 10.0.1.23", GATEWAY_AND_LB, "198.51.100.7"),
            ("空条目跳过", "127.0.0.1", "198.51.100.7,, 10.0.1.23,", GATEWAY_AND_LB, "198.51.100.7"),
            ("没配可信网段：永远是对端", "127.0.0.1", "198.51.100.7", (), "127.0.0.1"),
            ("对端解析不了：原样返回", "testclient", "198.51.100.7", GATEWAY_AND_LB, "testclient"),
            ("没有对端", None, "198.51.100.7", GATEWAY_AND_LB, "unknown"),
        ]
        for label, peer, xff, trusted, expected in cases:
            with self.subTest(label):
                self.assertEqual(real_client_ip(peer, xff, trusted), expected)

    def test_trusted_hop_walk_is_bounded(self) -> None:
        xff = ", ".join(["10.0.0.9"] * 4000)
        self.assertEqual(real_client_ip("127.0.0.1", xff, GATEWAY_AND_LB), "10.0.0.9")


class ResolverTests(unittest.TestCase):
    def test_parse_networks_reports_invalid(self) -> None:
        nets, invalid = parse_networks(["10.0.0.0/16", "nope", "198.51.100.7"])
        self.assertEqual([str(n) for n in nets], ["10.0.0.0/16", "198.51.100.7/32"])
        self.assertEqual(invalid, ["nope"])

    def test_inactive_without_trusted_proxies(self) -> None:
        resolver = IpResolver((), use_real_ip=True)
        self.assertFalse(resolver.active)
        self.assertIsNone(resolver.override("127.0.0.1", Headers({"x-forwarded-for": "198.51.100.7"})))

    def test_inactive_when_switch_off(self) -> None:
        resolver = IpResolver(GATEWAY_AND_LB, use_real_ip=False)
        self.assertTrue(resolver.configured)
        self.assertFalse(resolver.active)
        self.assertIsNone(resolver.override("127.0.0.1", Headers({"x-forwarded-for": "198.51.100.7"})))
        # 可信 IP 本身照样能算（白名单、guard 日志用）
        self.assertEqual(resolver.real_ip("127.0.0.1", "198.51.100.7, 10.0.1.23"), "198.51.100.7")

    def test_active_override(self) -> None:
        resolver = IpResolver(GATEWAY_AND_LB, use_real_ip=True)
        headers = Headers({"x-forwarded-for": "192.0.2.66, 198.51.100.7, 10.0.1.23"})
        self.assertEqual(resolver.override("127.0.0.1", headers), "198.51.100.7")

    def test_xff_of_joins_repeated_headers(self) -> None:
        headers = Headers(raw=[(b"x-forwarded-for", b"192.0.2.66"), (b"x-forwarded-for", b"198.51.100.7, 10.0.1.23")])
        self.assertEqual(xff_of(headers), "192.0.2.66, 198.51.100.7, 10.0.1.23")
        self.assertIsNone(xff_of(Headers({})))

    def test_legacy_ip_key_is_old_behavior(self) -> None:
        cases = [
            (("10.0.1.23", "198.51.100.1, 203.0.113.9"), "198.51.100.1"),  # 最左一个
            (("10.0.1.23", " 198.51.100.1 "), "198.51.100.1"),
            (("10.0.1.23", ", 203.0.113.9"), "10.0.1.23"),  # 最左为空 → 对端
            (("10.0.1.23", None), "10.0.1.23"),
            ((None, None), "unknown"),
            (("  ", ""), "unknown"),
        ]
        for (direct, forwarded), expected in cases:
            with self.subTest(direct=direct, forwarded=forwarded):
                self.assertEqual(legacy_ip_key(direct, forwarded), expected)


def _request(app: FastAPI | None, peer: str, xff: str | None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    scope = {"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 50000)}
    if app is not None:
        scope["app"] = app
    return Request(scope)


class HttpCallSiteTests(unittest.TestCase):
    """diagnostics / asr_corrections 的 _client_ip：解析器未生效时逐字是旧取法（最左值）。"""

    XFF = "192.0.2.66, 198.51.100.7, 10.0.1.23"

    def test_bare_app_keeps_leftmost(self) -> None:
        for func in (diagnostics._client_ip, asr_corrections._client_ip):
            with self.subTest(func=func.__module__):
                self.assertIsNone(request_ip_override(_request(None, "127.0.0.1", self.XFF)))
                self.assertEqual(func(_request(None, "127.0.0.1", self.XFF)), "192.0.2.66")

    def test_inactive_resolver_keeps_leftmost(self) -> None:
        app = FastAPI()
        app.state.ip_resolver = IpResolver(GATEWAY_AND_LB, use_real_ip=False)
        for func in (diagnostics._client_ip, asr_corrections._client_ip):
            with self.subTest(func=func.__module__):
                self.assertEqual(func(_request(app, "127.0.0.1", self.XFF)), "192.0.2.66")

    def test_active_resolver_uses_trusted_ip(self) -> None:
        app = FastAPI()
        app.state.ip_resolver = IpResolver(GATEWAY_AND_LB, use_real_ip=True)
        for func in (diagnostics._client_ip, asr_corrections._client_ip):
            with self.subTest(func=func.__module__):
                self.assertEqual(func(_request(app, "127.0.0.1", self.XFF)), "198.51.100.7")


class RateLimitIpTests(unittest.TestCase):
    """RateLimitMiddleware 的取 IP：解析器在构造时传入。"""

    def _client(self, resolver: IpResolver | None) -> TestClient:
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, requests_per_minute=6, burst=2, ip_resolver=resolver)

        @app.get("/api/x")
        async def endpoint():
            return {"ok": True}

        return TestClient(app, client=("127.0.0.1", 50000))

    def _spoofed(self, client: TestClient, n: int) -> list[int]:
        # 每次换一个伪造的最左值，真实链路（客户端 → 负载均衡 → 网关）不变
        return [
            client.get("/api/x", headers={"x-forwarded-for": f"192.0.2.{i}, 198.51.100.7, 10.0.1.23"}).status_code
            for i in range(1, n + 1)
        ]

    def test_without_resolver_each_spoofed_ip_gets_its_own_bucket(self) -> None:
        self.assertEqual(self._spoofed(self._client(None), 4), [200, 200, 200, 200])

    def test_inactive_resolver_is_old_behavior(self) -> None:
        self.assertEqual(self._spoofed(self._client(IpResolver(GATEWAY_AND_LB, use_real_ip=False)), 4), [200] * 4)

    def test_active_resolver_shares_one_bucket(self) -> None:
        self.assertEqual(self._spoofed(self._client(IpResolver(GATEWAY_AND_LB, use_real_ip=True)), 4)[-1], 429)


if __name__ == "__main__":
    unittest.main()
