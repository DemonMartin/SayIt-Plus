"""guard 第二版：会话频率、空结果、source 白名单、网页体验 Origin、网页体验全站上限。

时钟注入，阈值是随手取的小数字，IP 用 RFC 5737 文档段。
"""
from __future__ import annotations

import unittest

from backend.app.config import GuardAdmissionConfig, GuardConfig, GuardQuotaConfig, GuardWebDemoConfig
from backend.app.guard import GENERIC_REJECT_MESSAGE, STATUS_REJECTED_QUOTA, Guard, is_empty_result
from backend.app.quota import RATE_DEVICE_1H, RATE_IP_1H

GOOD_DEVICE = "sayit-0f1e2d3c-4b5a-4968-8778-a1b2c3d4e5f6"
OTHER_DEVICE = "sayit-11111111-2222-4333-8444-555555555555"
GOOD_META = {"device_id": GOOD_DEVICE, "client_version": "9.8.7"}
GATEWAY = "127.0.0.1"
CLIENT = "198.51.100.7"
XFF = f"{CLIENT}, 10.0.1.23"
TRUSTED = ["127.0.0.1/32", "10.0.0.0/16"]
SITE = "https://site.example"


class Clock:
    def __init__(self) -> None:
        self.wall = 1_800_000_000.0
        self.mono = 1000.0

    def advance(self, sec: float) -> None:
        self.wall += sec
        self.mono += sec


def _guard(clock: Clock, *, use_real_ip: bool = True, **kwargs) -> Guard:
    cfg = GuardConfig(present=True, trusted_proxies=TRUSTED, use_real_ip=use_real_ip, **kwargs)
    return Guard(cfg, clock=lambda: clock.wall, mono=lambda: clock.mono)


def _conn(g: Guard, *, web_demo: bool = False, xff: str = XFF, origin: str | None = None, cid: str = "c1"):
    return g.connection(cid=cid, peer=GATEWAY, forwarded=xff, xff=xff, is_web_demo=web_demo, origin=origin, ua="t")


def _start(conn, *, source: str = "live", meta=GOOD_META):
    return conn.check_start(source=source, client_meta=meta, compare=False, sid="s")


class DesktopRateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def test_per_minute_limit_rejects_and_recovers(self) -> None:
        g = _guard(self.clock, quota=GuardQuotaConfig(mode="enforce", device_sessions_per_minute=3))
        conn = _conn(g)
        for _ in range(3):
            self.assertIsNone(_start(conn))
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            rejection = _start(conn)
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.status, STATUS_REJECTED_QUOTA)
        self.assertFalse(rejection.close)  # 连接不断
        self.assertEqual(rejection.payload["code"], "quota")
        self.assertIn("rate limit", rejection.payload["message"])
        self.assertIn("guard reject dim=rate.device_1m mode=enforce", cm.output[0])
        self.clock.advance(66)  # 1 分钟窗口 + 一个 5 秒桶
        self.assertIsNone(_start(conn))

    def test_rejected_starts_do_not_count(self) -> None:
        g = _guard(self.clock, quota=GuardQuotaConfig(mode="enforce", device_sessions_per_hour=2))
        conn = _conn(g)
        self.assertIsNone(_start(conn))
        self.assertIsNone(_start(conn))
        for _ in range(5):
            self.assertIsNotNone(_start(conn))
        self.assertEqual(g.ledger.rate[RATE_DEVICE_1H].total(GOOD_DEVICE, g.now_ms()), 2)

    def test_limits_are_per_device(self) -> None:
        g = _guard(self.clock, quota=GuardQuotaConfig(mode="enforce", device_sessions_per_minute=1))
        self.assertIsNone(_start(_conn(g)))
        self.assertIsNotNone(_start(_conn(g)))
        self.assertIsNone(_start(_conn(g), meta={"device_id": OTHER_DEVICE, "client_version": "9.8.7"}))

    def test_ip_limit_catches_rotating_device_ids(self) -> None:
        g = _guard(self.clock, quota=GuardQuotaConfig(mode="enforce", ip_sessions_per_hour=3))
        for i in range(3):
            meta = {"device_id": f"sayit-0000000{i}-2222-4333-8444-555555555555", "client_version": "1.0.0"}
            self.assertIsNone(_start(_conn(g), meta=meta))
        meta = {"device_id": "sayit-00000009-2222-4333-8444-555555555555", "client_version": "1.0.0"}
        self.assertIsNotNone(_start(_conn(g), meta=meta))
        self.assertEqual(g.ledger.rate[RATE_IP_1H].total(CLIENT, g.now_ms()), 3)

    def test_ip_limit_only_logs_until_use_real_ip(self) -> None:
        g = _guard(self.clock, use_real_ip=False, quota=GuardQuotaConfig(mode="enforce", ip_sessions_per_hour=1))
        self.assertIsNone(_start(_conn(g)))
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            self.assertIsNone(_start(_conn(g)))
        self.assertIn("guard violation dim=rate.ip_1h", cm.output[0])
        self.assertIn("ip_untrusted", cm.output[0])

    def test_log_mode_counts_but_never_rejects(self) -> None:
        g = _guard(self.clock, quota=GuardQuotaConfig(mode="log", device_sessions_per_minute=1))
        conn = _conn(g)
        self.assertIsNone(_start(conn))
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            self.assertIsNone(_start(conn))
        self.assertIn("guard violation dim=rate.device_1m mode=log", cm.output[0])

    def test_whitelist_is_not_rate_limited(self) -> None:
        g = _guard(self.clock, allow_ips=[CLIENT], quota=GuardQuotaConfig(mode="enforce", ip_sessions_per_hour=1))
        for _ in range(5):
            self.assertIsNone(_start(_conn(g), source="meeting_file", meta={}))

    def test_off_counts_nothing(self) -> None:
        g = _guard(self.clock)
        for _ in range(5):
            self.assertIsNone(_start(_conn(g)))
        self.assertEqual(sum(len(w) for w in g.ledger.rate.values()), 0)


class EmptyResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def test_classifier(self) -> None:
        for text in ("", "  ", "嗯。", "啊！", "…", "。，", "a"):
            with self.subTest(text=text):
                self.assertTrue(is_empty_result(text))
        for text in ("你好", "OK", "12", "嗯嗯", "你好，Blender"):
            with self.subTest(text=text):
                self.assertFalse(is_empty_result(text))

    def test_empty_results_block_next_start(self) -> None:
        g = _guard(self.clock, quota=GuardQuotaConfig(mode="enforce", device_empty_per_hour=2))
        conn = _conn(g)
        for text in ("嗯。", "今天天气不错", ""):
            self.assertIsNone(_start(conn))
            conn.note_result(text)
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            rejection = _start(conn)
        self.assertIsNotNone(rejection)
        self.assertIn("rate.empty_device_1h", cm.output[0])
        self.clock.advance(3661)
        self.assertIsNone(_start(conn))

    def test_web_demo_empty_results_are_not_counted_per_device(self) -> None:
        g = _guard(self.clock, quota=GuardQuotaConfig(mode="enforce", device_empty_per_hour=1))
        conn = _conn(g, web_demo=True)
        for _ in range(3):
            self.assertIsNone(_start(conn, meta={}))
            conn.note_result("")


class SourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def test_desktop_custom_source_rejected(self) -> None:
        g = _guard(self.clock, admission=GuardAdmissionConfig(mode="enforce"))
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            rejection = _start(_conn(g), source="sailchat_blender_wake")
        self.assertIsNotNone(rejection)
        self.assertTrue(rejection.close)
        self.assertEqual(rejection.payload["message"], GENERIC_REJECT_MESSAGE)
        self.assertIn("guard reject dim=admission.source", cm.output[0])
        self.assertIn("detail=source=sailchat_blender_wake", cm.output[0])

    def test_desktop_real_sources_pass(self) -> None:
        g = _guard(self.clock, admission=GuardAdmissionConfig(mode="enforce"))
        for source in ("live", "history_reprocess"):
            with self.subTest(source=source):
                self.assertIsNone(_start(_conn(g), source=source))

    def test_web_demo_only_live(self) -> None:
        g = _guard(self.clock, admission=GuardAdmissionConfig(mode="enforce"))
        self.assertIsNone(_start(_conn(g, web_demo=True), meta={}))
        self.assertIsNotNone(_start(_conn(g, web_demo=True), source="history_reprocess", meta={}))

    def test_whitelist_custom_source_passes(self) -> None:
        g = _guard(self.clock, allow_ips=[CLIENT], admission=GuardAdmissionConfig(mode="enforce"))
        self.assertIsNone(_start(_conn(g), source="meeting_file", meta={}))

    def test_log_mode_only_logs(self) -> None:
        g = _guard(self.clock, admission=GuardAdmissionConfig(mode="log"))
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            self.assertIsNone(_start(_conn(g), source="sailchat_blender_wake"))
        self.assertIn("guard violation dim=admission.source", cm.output[0])


class WebDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def _g(self, mode: str = "enforce", **quota) -> Guard:
        return _guard(
            self.clock,
            web_demo=GuardWebDemoConfig(mode=mode, allowed_origins=[SITE + "/", "https://dev.site.example"]),
            quota=GuardQuotaConfig(mode="enforce", **quota) if quota else GuardQuotaConfig(),
        )

    def test_origin_allowed(self) -> None:
        g = self._g()
        for origin in (SITE, "HTTPS://SITE.EXAMPLE", "https://dev.site.example"):
            with self.subTest(origin=origin):
                self.assertIsNone(_conn(g, web_demo=True, origin=origin).check_connect())

    def test_origin_missing_or_foreign_rejected(self) -> None:
        g = self._g()
        for origin in (None, "", "null", "https://evil.example", "https://site.example.evil.example"):
            with self.subTest(origin=origin):
                rejection = _conn(g, web_demo=True, origin=origin).check_connect()
                self.assertIsNotNone(rejection)
                self.assertTrue(rejection.close)
                self.assertEqual(rejection.payload["message"], GENERIC_REJECT_MESSAGE)

    def test_origin_log_mode_and_desktop_unaffected(self) -> None:
        g = self._g(mode="log")
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            self.assertIsNone(_conn(g, web_demo=True, origin="https://evil.example").check_connect())
        self.assertIn("guard violation dim=web_demo.origin mode=log", cm.output[0])
        g = self._g()
        self.assertIsNone(_conn(g, origin=None).check_connect())  # 桌面端不查 Origin

    def test_whitelist_skips_origin(self) -> None:
        g = _guard(self.clock, allow_ips=[CLIENT], web_demo=GuardWebDemoConfig(mode="enforce", allowed_origins=[SITE]))
        self.assertIsNone(_conn(g, web_demo=True, origin=None).check_connect())

    def test_per_ip_hourly_sessions(self) -> None:
        g = self._g(demo_ip_sessions_per_hour=3)
        conn = _conn(g, web_demo=True, origin=SITE)
        for _ in range(3):
            self.assertIsNone(_start(conn, meta={}))
        rejection = _start(conn, meta={})
        self.assertIsNotNone(rejection)
        self.assertFalse(rejection.close)
        self.assertIn("网页体验次数已用完", rejection.payload["message"])
        other = _conn(g, web_demo=True, origin=SITE, xff="203.0.113.50, 10.0.1.23")
        self.assertIsNone(_start(other, meta={}))  # 别的 IP 不受影响
        self.clock.advance(3661)
        self.assertIsNone(_start(conn, meta={}))

    def test_site_wide_sessions_pause_demo_only(self) -> None:
        g = self._g(demo_global_sessions_per_hour=2)
        self.assertIsNone(_start(_conn(g, web_demo=True, xff="203.0.113.1, 10.0.1.23"), meta={}))
        self.assertIsNone(_start(_conn(g, web_demo=True, xff="203.0.113.2, 10.0.1.23"), meta={}))
        rejection = _start(_conn(g, web_demo=True, xff="203.0.113.3, 10.0.1.23"), meta={})
        self.assertIsNotNone(rejection)
        self.assertIn("网页体验人数较多", rejection.payload["message"])
        self.assertIsNone(_start(_conn(g)))  # 桌面端照常

    def test_site_wide_minutes_pause_demo_only(self) -> None:
        g = self._g(demo_global_minutes=1)
        conn = _conn(g, web_demo=True, xff="203.0.113.1, 10.0.1.23")
        self.assertIsNone(_start(conn, meta={}))
        conn.charge_audio(60_000)
        rejection = _start(_conn(g, web_demo=True, xff="203.0.113.9, 10.0.1.23"), meta={})
        self.assertIsNotNone(rejection)
        self.assertIn("网页体验人数较多", rejection.payload["message"])
        self.assertIsNone(_start(_conn(g)))

    def test_site_wide_limits_hold_even_without_trusted_ip(self) -> None:
        g = _guard(self.clock, use_real_ip=False, quota=GuardQuotaConfig(mode="enforce", demo_global_sessions_per_hour=1))
        self.assertIsNone(_start(_conn(g, web_demo=True), meta={}))
        self.assertIsNotNone(_start(_conn(g, web_demo=True), meta={}))

    def test_banner_lists_new_settings(self) -> None:
        line = self._g(demo_ip_sessions_per_hour=10, device_sessions_per_minute=30).banner_line()
        self.assertIn("rate=(device_1m=30,demo_ip_1h=10)", line)
        self.assertIn("web_demo_origin=enforce(origins=2)", line)


if __name__ == "__main__":
    unittest.main()
