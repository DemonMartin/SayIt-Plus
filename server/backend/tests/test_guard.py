"""guard：推流检测、准入、音频额度、润色预算、白名单、日志 / 统计 / banner。

时钟全部注入（wall 毫秒 + monotonic 秒），阈值是随手取的小数字，IP 用 RFC 5737 文档段。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.app.config import (
    GuardAdmissionConfig,
    GuardAiBudgetConfig,
    GuardAlertConfig,
    GuardConfig,
    GuardPaceConfig,
    GuardQuotaConfig,
)
from backend.app.db import Database
from backend.app.guard import (
    GENERIC_REJECT_MESSAGE,
    STATUS_REJECTED_ADMISSION,
    STATUS_REJECTED_PACE,
    STATUS_REJECTED_QUOTA,
    Guard,
    human_wait,
)
from backend.app.quota import DIM_DEVICE, DIM_INTERNAL, DIM_IP, DIM_NONLIVE_IP, WINDOW_MS

GOOD_DEVICE = "sayit-0f1e2d3c-4b5a-4968-8778-a1b2c3d4e5f6"
GOOD_META = {"device_id": GOOD_DEVICE, "client_version": "9.8.7"}
GATEWAY = "127.0.0.1"
CLIENT = "198.51.100.7"
XFF = f"{CLIENT}, 10.0.1.23"
TRUSTED = ["127.0.0.1/32", "10.0.0.0/16"]


class Clock:
    def __init__(self) -> None:
        self.wall = 1_800_000_000.0
        self.mono = 1000.0

    def advance(self, sec: float) -> None:
        self.wall += sec
        self.mono += sec


def _guard(clock: Clock, **kwargs) -> Guard:
    cfg = GuardConfig(present=True, **kwargs)
    return Guard(cfg, clock=lambda: clock.wall, mono=lambda: clock.mono)


def _conn(g: Guard, *, xff: str | None = XFF, peer: str = GATEWAY, web_demo: bool = False, cid: str = "c1"):
    return g.connection(cid=cid, peer=peer, forwarded=xff, xff=xff, is_web_demo=web_demo)


def _pcm(seconds: float) -> int:
    return int(seconds * 32000)


class PaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def _start(self, mode: str, *, source: str = "live", **conn_kwargs):
        g = _guard(self.clock, pace=GuardPaceConfig(mode=mode, max_ratio=1.5, grace_sec=3.0),
                   trusted_proxies=TRUSTED, **conn_kwargs.pop("cfg", {}))
        conn = _conn(g, **conn_kwargs)
        self.assertIsNone(conn.check_start(source=source, client_meta=GOOD_META, compare=False, sid="s1"))
        return g, conn

    def test_log_mode_writes_one_line_and_never_rejects(self) -> None:
        g, conn = self._start("log")
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            self.assertIsNone(conn.check_pace(_pcm(10)))
            self.assertIsNone(conn.check_pace(_pcm(20)))
        self.assertEqual(len(cm.output), 1)
        self.assertIn("guard violation dim=pace mode=log", cm.output[0])
        self.assertIn("src=live", cm.output[0])

    def test_enforce_returns_generic_rejection(self) -> None:
        g, conn = self._start("enforce")
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            rejection = conn.check_pace(_pcm(10))
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.status, STATUS_REJECTED_PACE)
        self.assertTrue(rejection.close)
        self.assertEqual(rejection.payload, {"type": "error", "code": "rejected", "message": GENERIC_REJECT_MESSAGE})
        self.assertIn("guard reject dim=pace mode=enforce", cm.output[0])

    def test_boundary_counts_from_start(self) -> None:
        g, conn = self._start("enforce")
        self.clock.advance(4.0)  # 允许 4 × 1.5 + 3 = 9 秒
        self.assertIsNone(conn.check_pace(_pcm(9.0)))  # 刚好相等不算
        self.assertIsNotNone(conn.check_pace(_pcm(9.01)))

    def test_realtime_stream_passes(self) -> None:
        g, conn = self._start("enforce")
        for i in range(1, 121):
            self.clock.advance(0.1)
            self.assertIsNone(conn.check_pace(_pcm(i * 0.1)))

    def test_connection_budget_catches_many_short_bursts(self) -> None:
        g, conn = self._start("enforce")
        self.assertIsNone(conn.check_pace(_pcm(2.5)))  # 单看这一段没超余量
        conn.end_session()
        self.clock.advance(0.2)
        self.assertIsNone(conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s2"))
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            rejection = conn.check_pace(_pcm(2.5))  # 连接累计 5 秒 > 0.2 × 1.5 + 3
        self.assertIsNotNone(rejection)
        self.assertIn("scope=connection", cm.output[0])

    def test_connection_budget_never_trips_on_realtime_sessions_with_gaps(self) -> None:
        g, conn = self._start("enforce")
        for n in range(20):
            if n:
                self.assertIsNone(conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid=f"s{n}"))
            for i in range(1, 51):  # 每段 5 秒，按实时推
                self.clock.advance(0.1)
                self.assertIsNone(conn.check_pace(_pcm(i * 0.1)))
            conn.end_session()
            self.clock.advance(1.0)

    def test_resume_only_reuses_an_open_session(self) -> None:
        g = _guard(self.clock, admission=GuardAdmissionConfig(mode="enforce"), trusted_proxies=TRUSTED)
        conn = _conn(g)
        self.assertIsNotNone(conn.check_resume())  # 从没 start 过
        conn = _conn(g, cid="c2")
        self.assertIsNone(conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s"))
        self.assertIsNone(conn.check_resume())  # 会话里的 pause / resume 照常
        conn.end_session()
        rejection = conn.check_resume()  # 会话结束后不能再复用
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.status, STATUS_REJECTED_ADMISSION)
        self.assertTrue(rejection.close)

    def test_nonlive_demo_and_whitelist_are_not_checked(self) -> None:
        for label, kwargs in (
            ("nonlive", {"source": "history_reprocess"}),
            ("live uppercase is nonlive", {"source": "LIVE"}),
            ("web demo", {"web_demo": True}),
            ("whitelist", {"cfg": {"allow_ips": [CLIENT]}}),
        ):
            with self.subTest(label):
                g, conn = self._start("enforce", **kwargs)
                self.assertIsNone(conn.check_pace(_pcm(60)))

    def test_zero_ratio_disables_pace_with_warning(self) -> None:
        g = _guard(self.clock, pace=GuardPaceConfig(mode="enforce", max_ratio=0, grace_sec=3))
        self.assertEqual(g.pace_mode, "off")
        self.assertTrue(any("max_ratio" in w for w in g.warnings))


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def _guard(self, mode: str, **kwargs) -> Guard:
        return _guard(self.clock, admission=GuardAdmissionConfig(mode=mode), trusted_proxies=TRUSTED, **kwargs)

    def test_bad_formats_rejected_at_start(self) -> None:
        bad = {
            "device_id": {"device_id": "my-device", "client_version": "9.8.7"},
            "uppercase uuid": {"device_id": GOOD_DEVICE.upper().replace("SAYIT", "sayit"), "client_version": "9.8.7"},
            "empty meta": {},
            "too long": {"device_id": GOOD_DEVICE + "0" * 200, "client_version": "9.8.7"},
            "version": {"device_id": GOOD_DEVICE, "client_version": "9.8"},
            "version suffix": {"device_id": GOOD_DEVICE, "client_version": "9.8.7-beta"},
            "non-ascii digits": {"device_id": GOOD_DEVICE, "client_version": "\u0661.\u0662.\u0663"},
        }
        g = self._guard("enforce")
        for label, meta in bad.items():
            with self.subTest(label):
                rejection = _conn(g).check_start(source="live", client_meta=meta, compare=False, sid="s")
                self.assertIsNotNone(rejection)
                self.assertEqual(rejection.status, STATUS_REJECTED_ADMISSION)
                self.assertEqual(rejection.payload["message"], GENERIC_REJECT_MESSAGE)
                self.assertEqual(rejection.payload["code"], "rejected")
                self.assertTrue(rejection.close)

    def test_good_meta_passes(self) -> None:
        g = self._guard("enforce")
        for source in ("live", "history_reprocess"):
            with self.subTest(source):
                self.assertIsNone(_conn(g).check_start(source=source, client_meta=GOOD_META, compare=False, sid="s"))

    def test_log_mode_only_logs(self) -> None:
        g = self._guard("log")
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            self.assertIsNone(_conn(g).check_start(source="live", client_meta={}, compare=False, sid="s"))
        self.assertIn("guard violation dim=admission.device_id mode=log", cm.output[0])

    def test_web_demo_and_whitelist_skip(self) -> None:
        g = self._guard("enforce", allow_ips=[CLIENT])
        self.assertIsNone(_conn(g).check_start(source="live", client_meta={}, compare=False, sid="s"))
        g = self._guard("enforce")
        conn = _conn(g, web_demo=True)
        self.assertIsNone(conn.check_start(source="live", client_meta={}, compare=False, sid="s"))
        self.assertIsNone(conn.check_stop(audio_ms=4500, ptt_hold_ms=None))

    def test_live_without_ptt_rejected_at_stop(self) -> None:
        g = self._guard("enforce")
        conn = _conn(g)
        self.assertIsNone(conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s"))
        self.assertIsNone(conn.check_stop(audio_ms=4500, ptt_hold_ms=0))  # 带了就行
        self.assertIsNone(conn.check_stop(audio_ms=0, ptt_hold_ms=None))  # 没音频不查
        rejection = conn.check_stop(audio_ms=4500, ptt_hold_ms=None)
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.status, STATUS_REJECTED_ADMISSION)
        self.assertEqual(rejection.payload["message"], GENERIC_REJECT_MESSAGE)

    def test_nonlive_without_ptt_passes(self) -> None:
        g = self._guard("enforce")
        conn = _conn(g)
        self.assertIsNone(conn.check_start(source="history_reprocess", client_meta=GOOD_META, compare=False, sid="s"))
        self.assertIsNone(conn.check_stop(audio_ms=4500, ptt_hold_ms=None))

    def test_device_id_with_control_chars_cannot_split_log_lines(self) -> None:
        g = self._guard("log")
        meta = {"device_id": "evil\nguard reject dim=fake ip=203.0.113.1", "client_version": "9.8.7"}
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            _conn(g).check_start(source="live\r\nx", client_meta=meta, compare=False, sid="s\n")
        self.assertEqual(len(cm.records), 1)
        message = cm.records[0].getMessage()
        self.assertNotIn("\n", message)
        self.assertNotIn("\r", message)
        self.assertEqual(message.count(" ip="), 1)


class QuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def _guard(self, *, mode: str = "enforce", use_real_ip: bool = True, **limits) -> Guard:
        return _guard(
            self.clock,
            quota=GuardQuotaConfig(mode=mode, **limits),
            trusted_proxies=TRUSTED,
            use_real_ip=use_real_ip,
            allow_ips=["203.0.113.5"],
        )

    def _session(self, g: Guard, audio_ms: int, *, source: str = "live", meta=GOOD_META, **conn_kwargs):
        conn = _conn(g, **conn_kwargs)
        rejection = conn.check_start(source=source, client_meta=meta, compare=False, sid="s")
        if rejection is None:
            conn.record(audio_ms=audio_ms, ai_tokens=0)
        return rejection

    def test_device_limit_rejects_next_start_with_retry_hint(self) -> None:
        g = self._guard(device_minutes=0.05)  # 3 秒
        self.assertIsNone(self._session(g, 2000))
        self.assertIsNone(self._session(g, 2000))  # 用前只用了 2 秒，放行；这次之后超了也不中途拒
        rejection = self._session(g, 2000)
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.status, STATUS_REJECTED_QUOTA)
        self.assertFalse(rejection.close)
        self.assertEqual(rejection.payload["type"], "error")
        self.assertEqual(rejection.payload["code"], "quota")
        self.assertIn("quota", rejection.payload["message"])
        self.assertRegex(rejection.payload["message"], r"约 \d+ (分钟|小时)后恢复$")

    def test_rolling_expiry(self) -> None:
        g = self._guard(device_minutes=0.05)
        self.assertIsNone(self._session(g, 4000))
        self.assertIsNotNone(self._session(g, 1000))
        self.clock.advance(WINDOW_MS / 1000 + 900)
        self.assertIsNone(self._session(g, 1000))

    def test_ip_limit_only_logs_until_use_real_ip_is_active(self) -> None:
        g = self._guard(use_real_ip=False, ip_minutes=0.05)
        self.assertIsNone(self._session(g, 4000))
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            self.assertIsNone(self._session(g, 1000))
        self.assertIn("guard violation dim=quota.ip mode=enforce", cm.output[0])
        self.assertIn("ip_untrusted", cm.output[0])
        self.assertIn(f"ip={CLIENT}", cm.output[0])  # 配了可信代理：日志里已经是可信 IP

        g = self._guard(use_real_ip=True, ip_minutes=0.05)
        self.assertIsNone(self._session(g, 4000))
        self.assertIsNotNone(self._session(g, 1000, meta={**GOOD_META, "device_id": GOOD_DEVICE[:-1] + "0"}))

    def test_nonlive_sub_limit_and_totals(self) -> None:
        g = self._guard(nonlive_device_minutes=0.05, ip_minutes=0.1)
        self.assertIsNone(self._session(g, 4000, source="history_reprocess"))
        # 非 live 子额度满了：非 live 被拒，live 照常
        self.assertIsNotNone(self._session(g, 1000, source="history_reprocess"))
        self.assertIsNone(self._session(g, 1000, source="live"))
        # 非 live 同时计进了 IP 总额：4 + 1 秒 < 6 秒，再来 2 秒之后 live 也被拒
        self.assertIsNone(self._session(g, 2000, source="live"))
        self.assertIsNotNone(self._session(g, 1000, source="live"))
        self.assertEqual(g.ledger.audio[DIM_IP].total(CLIENT, g.now_ms()), 7000)
        self.assertEqual(g.ledger.audio[DIM_NONLIVE_IP].total(CLIENT, g.now_ms()), 4000)

    def test_web_demo_uses_demo_bucket_only(self) -> None:
        g = self._guard(demo_ip_minutes=0.05, ip_minutes=0.05)
        self.assertIsNone(self._session(g, 4000, web_demo=True))
        self.assertIsNotNone(self._session(g, 1000, web_demo=True))
        self.assertIsNone(self._session(g, 1000))  # 桌面端不受网页体验额度影响
        self.assertEqual(len(g.ledger.audio[DIM_DEVICE]), 1)

    def test_whitelist_uses_internal_bucket_only(self) -> None:
        g = self._guard(internal_minutes=0.05, device_minutes=0.05, ip_minutes=0.05)
        wl_xff = "203.0.113.5, 10.0.1.23"
        self.assertIsNone(self._session(g, 4000, xff=wl_xff))
        self.assertIsNotNone(self._session(g, 1000, xff=wl_xff))
        self.assertEqual(g.ledger.audio[DIM_INTERNAL].total("203.0.113.5", g.now_ms()), 4000)
        self.assertEqual(len(g.ledger.audio[DIM_DEVICE]), 0)
        self.assertIsNone(self._session(g, 1000))  # 普通用户的桶没被白名单占用

    def test_whitelist_matches_trusted_ip_even_when_use_real_ip_is_off(self) -> None:
        g = self._guard(use_real_ip=False)
        self.assertTrue(_conn(g, xff="203.0.113.5, 10.0.1.23").whitelisted)
        # 伪造最左值进不了白名单
        self.assertFalse(_conn(g, xff="203.0.113.5, 198.51.100.7, 10.0.1.23").whitelisted)

    def test_compare_counts_twice(self) -> None:
        g = self._guard(device_minutes=1)
        conn = _conn(g)
        conn.check_start(source="live", client_meta=GOOD_META, compare=True, sid="s")
        conn.record(audio_ms=1500, ai_tokens=0)
        self.assertEqual(g.ledger.audio[DIM_DEVICE].total(GOOD_DEVICE, g.now_ms()), 3000)

    def test_message_wait_formatting(self) -> None:
        self.assertEqual(human_wait(1), "1 分钟")
        self.assertEqual(human_wait(59 * 60_000 + 1), "60 分钟")
        self.assertEqual(human_wait(3 * 3_600_000 + 60_000), "3 小时")
        self.assertEqual(human_wait(WINDOW_MS + 300_000), "24 小时")


class AiBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def _conn(self, mode: str, *, use_real_ip: bool = True, **limits):
        g = _guard(self.clock, ai_budget=GuardAiBudgetConfig(mode=mode, **limits), trusted_proxies=TRUSTED,
                   use_real_ip=use_real_ip)
        return g, _conn(g)

    def test_enforce_blocks_once_budget_is_used(self) -> None:
        g, conn = self._conn("enforce", device_tokens=10)
        conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s")
        self.assertFalse(conn.ai_blocked())
        conn.record(audio_ms=1000, ai_tokens=12)
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            self.assertTrue(conn.ai_blocked())
        self.assertIn("guard degrade dim=ai.device mode=enforce", cm.output[0])
        self.assertEqual(g.ledger.ai[DIM_IP].total(CLIENT, g.now_ms()), 12)

    def test_log_mode_never_blocks(self) -> None:
        g, conn = self._conn("log", ip_tokens=5)
        conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s")
        conn.record(audio_ms=1000, ai_tokens=6)
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            self.assertFalse(conn.ai_blocked())
        self.assertIn("guard violation dim=ai.ip mode=log", cm.output[0])

    def test_ip_budget_waits_for_trusted_ip(self) -> None:
        g, conn = self._conn("enforce", use_real_ip=False, ip_tokens=5)
        conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s")
        conn.record(audio_ms=1000, ai_tokens=6)
        self.assertFalse(conn.ai_blocked())

    def test_prompt_cap_enforce_skips_polishing(self) -> None:
        g, conn = self._conn("enforce", max_system_prompt_chars=100)
        conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s")
        self.assertFalse(conn.ai_blocked(prompt_chars=100))  # 刚好等于上限不算
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            self.assertTrue(conn.ai_blocked(prompt_chars=101))
        self.assertIn("guard degrade dim=ai.prompt mode=enforce", cm.output[0])
        self.assertIn("detail=prompt=101,limit=100", cm.output[0])

    def test_prompt_cap_log_mode_only_logs(self) -> None:
        g, conn = self._conn("log", max_system_prompt_chars=100)
        conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s")
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            self.assertFalse(conn.ai_blocked(prompt_chars=5000))
        self.assertIn("guard violation dim=ai.prompt mode=log", cm.output[0])

    def test_prompt_cap_zero_means_unlimited_and_whitelist_is_exempt(self) -> None:
        g, conn = self._conn("enforce", device_tokens=10**9)
        conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s")
        self.assertFalse(conn.ai_blocked(prompt_chars=10**6))
        g = _guard(self.clock, ai_budget=GuardAiBudgetConfig(mode="enforce", max_system_prompt_chars=100),
                   trusted_proxies=TRUSTED, use_real_ip=True, allow_ips=[CLIENT])
        conn = _conn(g)
        conn.check_start(source="meeting_file", client_meta={}, compare=False, sid="s")
        self.assertTrue(conn.whitelisted)
        self.assertFalse(conn.ai_blocked(prompt_chars=5000))

    def test_budget_counts_tokens_and_banner_shows_units(self) -> None:
        g, conn = self._conn("enforce", device_tokens=1000, max_system_prompt_chars=8000)
        conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s")
        conn.charge_ai(999)
        self.assertFalse(conn.ai_blocked(prompt_chars=10))
        conn.charge_ai(1)
        self.assertTrue(conn.ai_blocked(prompt_chars=10))
        self.assertIn("ai_budget=enforce(device=1000tok,max_prompt=8000chars)", g.banner_line())
        self.assertIn("ai_tokens=1000", g.stats_line())


class ObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def test_missing_section_does_nothing(self) -> None:
        g = Guard(GuardConfig(), clock=lambda: self.clock.wall, mono=lambda: self.clock.mono)
        conn = g.connection(cid="c", peer=GATEWAY, forwarded=XFF, xff=XFF, is_web_demo=False)
        self.assertIsNone(conn.check_start(source="live", client_meta={}, compare=True, sid="s"))
        self.assertIsNone(conn.check_pace(_pcm(900)))
        self.assertIsNone(conn.check_stop(audio_ms=4500, ptt_hold_ms=None))
        self.assertFalse(conn.ai_blocked())
        conn.record(audio_ms=4500, ai_tokens=450)
        self.assertEqual(g.ledger.tracked(), 0)
        self.assertFalse(g.resolver.active)
        self.assertTrue(g.banner_line().startswith("guard banner configured=no"))
        self.assertIsNone(g.seed_from_db(object()))

    def test_banner_lists_modes_thresholds_and_warnings(self) -> None:
        g = _guard(
            self.clock,
            pace=GuardPaceConfig(mode="log", max_ratio=1.5, grace_sec=3),
            quota=GuardQuotaConfig(mode="enforce", device_minutes=7),
            use_real_ip=True,
            allow_ips=["203.0.113.5", "not-an-ip"],
        )
        line = g.banner_line()
        for needle in ("configured=yes", "pace=log(max_ratio=1.5,grace_sec=3)", "admission=off",
                       "quota=enforce(device=7.0min)", "ai_budget=off", "use_real_ip=ignored", "ip_dims=log_only"):
            with self.subTest(needle=needle):
                self.assertIn(needle, line)
        self.assertNotIn("\n", line)
        joined = " ".join(g.warnings)
        self.assertIn("use_real_ip", joined)
        self.assertIn("not-an-ip", joined)
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            g.log_banner()
        self.assertEqual(sum(1 for line in cm.output if "guard banner" in line), 1)

    def test_event_dedupe_keeps_counting(self) -> None:
        g = _guard(self.clock, admission=GuardAdmissionConfig(mode="log"), trusted_proxies=TRUSTED)
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            for _ in range(5):
                _conn(g).check_start(source="live", client_meta={}, compare=False, sid="s")
            self.clock.advance(61)
            _conn(g).check_start(source="live", client_meta={}, compare=False, sid="s")
        self.assertEqual(len(cm.output), 2)
        self.assertIn("suppressed=4", cm.output[1])
        self.assertIn("violation=admission:6", g.stats_line())

    def test_stats_line_and_reset(self) -> None:
        g = _guard(self.clock, quota=GuardQuotaConfig(mode="log", device_minutes=0.01), trusted_proxies=TRUSTED)
        conn = _conn(g)
        conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s")
        conn.record(audio_ms=90_000, ai_tokens=321)
        demo = _conn(g, web_demo=True)
        demo.check_start(source="live", client_meta={}, compare=False, sid="s2")
        demo.record(audio_ms=30_000, ai_tokens=0)
        _conn(g).check_start(source="history_reprocess", client_meta=GOOD_META, compare=False, sid="s3")
        line = g.stats_line()
        for needle in ("guard stats ", "sessions=live:1,nonlive:1,web_demo:1,internal:0",
                       "audio_min=live:1.5,nonlive:0.0,web_demo:0.5", "ai_tokens=321", "violation=quota:1",
                       f"device:1.5min@{GOOD_DEVICE}", f"ip:1.5min@{CLIENT}", "demo_ip:0.5min", "peak_hour=audio:2.0min,ai:321"):
            with self.subTest(needle=needle):
                self.assertIn(needle, line)
        again = g.stats_line()
        self.assertIn("sessions=live:0,nonlive:0,web_demo:0,internal:0", again)
        self.assertIn("ai_tokens=0", again)
        self.assertIn("violation=0", again)
        self.assertIn(f"device:1.5min@{GOOD_DEVICE}", again)  # 24 小时窗口不随周期清零

    def test_alert_at_most_once_per_hour(self) -> None:
        g = _guard(self.clock, alert=GuardAlertConfig(audio_minutes_per_hour=1), trusted_proxies=TRUSTED)
        conn = _conn(g)
        conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s")
        with self.assertLogs("sayit.guard", level="WARNING") as cm:
            conn.record(audio_ms=40_000, ai_tokens=0)
            conn.record(audio_ms=40_000, ai_tokens=0)
            conn.record(audio_ms=40_000, ai_tokens=0)
            self.clock.advance(3601)
            conn.record(audio_ms=70_000, ai_tokens=0)
        alerts = [line for line in cm.output if "guard alert" in line]
        self.assertEqual(len(alerts), 2)
        self.assertIn("dim=alert.audio", alerts[0])

    def test_netid_once_per_ip_per_ten_minutes(self) -> None:
        g = _guard(self.clock, trusted_proxies=TRUSTED, netid_log=True)
        with self.assertLogs("sayit.guard", level="INFO") as cm:
            _conn(g)
            _conn(g)
            _conn(g, xff=f"{CLIENT}, 10.0.2.99")  # 另一个入口（另一台负载均衡）
            self.clock.advance(601)
            _conn(g)
        lines = [line for line in cm.output if "guard netid" in line]
        self.assertEqual(len(lines), 3)
        self.assertIn(f"guard netid ip={CLIENT} peer={GATEWAY} xff={CLIENT},10.0.1.23 legacy={CLIENT}", lines[0])
        self.assertIn("trusted_peer=yes use_real_ip=off", lines[0])

    def test_netid_off_by_default(self) -> None:
        g = _guard(self.clock, trusted_proxies=TRUSTED)
        logger = logging.getLogger("sayit.guard")
        with self.assertLogs(logger, level="INFO") as cm:
            _conn(g)
            logger.info("sentinel")
        self.assertEqual(cm.output, ["INFO:sayit.guard:sentinel"])

    def test_stats_task_lifecycle(self) -> None:
        async def scenario() -> None:
            g = _guard(self.clock)
            g.start_stats_task()
            self.assertIsNotNone(g._task)
            await g.stop_stats_task()
            self.assertIsNone(g._task)
            absent = Guard(GuardConfig())
            absent.start_stats_task()
            self.assertIsNone(absent._task)

        asyncio.run(scenario())


class SeedFromDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self._tmp = Path(tempfile.mkdtemp(prefix="sayit-guard-seed-"))
        self.db = Database(str(self._tmp / "t.sqlite3"))
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _row(self, sid: str, ago_ms: int, audio_ms: int, *, forwarded: str | None, device: str | None = GOOD_DEVICE,
             source: str = "live") -> None:
        started = int(self.clock.wall * 1000) - ago_ms
        self.db.execute(
            "INSERT INTO usage_sessions(session_id, connection_id, started_at, device_id, client_ip, forwarded_for,"
            " source, audio_duration_ms, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, "cid", started, device, GATEWAY, forwarded, source, audio_ms, "success", started, started),
        )

    def test_recomputes_ip_with_the_live_function(self) -> None:
        g = _guard(self.clock, quota=GuardQuotaConfig(mode="enforce", device_minutes=0.05, ip_minutes=1),
                   trusted_proxies=TRUSTED, use_real_ip=True, allow_ips=["203.0.113.5"])
        self._row("a", 60_000, 2_000, forwarded="192.0.2.66, " + XFF)  # 伪造的最左值不算
        self._row("b", 30_000, 2_000, forwarded=XFF, source="history_reprocess")
        self._row("wl", 30_000, 5_000, forwarded="203.0.113.5, 10.0.1.23", device=None)
        self._row("truncated", 10_000, 1_000, forwarded="192.0.2.1, " + "9" * 700)
        result = g.seed_from_db(self.db)
        self.assertEqual((result.rows, result.seeded, result.ip_unknown), (4, 4, 1))
        now = g.now_ms()
        self.assertEqual(g.ledger.audio[DIM_IP].total(CLIENT, now), 4_000)
        self.assertEqual(g.ledger.audio[DIM_IP].total("192.0.2.66", now), 0)
        self.assertEqual(g.ledger.audio[DIM_DEVICE].total(GOOD_DEVICE, now), 5_000)
        self.assertEqual(g.ledger.audio[DIM_NONLIVE_IP].total(CLIENT, now), 2_000)
        self.assertEqual(g.ledger.audio[DIM_INTERNAL].total("203.0.113.5", now), 5_000)
        self.assertIn("seeded=4/4", g.banner_line())
        # 补回来的用量照样挡 start
        rejection = _conn(g).check_start(source="live", client_meta=GOOD_META, compare=False, sid="s")
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.status, STATUS_REJECTED_QUOTA)

    def test_db_errors_do_not_break_startup(self) -> None:
        g = _guard(self.clock)

        class Broken:
            def fetch_all(self, *_args, **_kwargs):
                raise RuntimeError("boom")

        with self.assertLogs("sayit.guard", level="WARNING"):
            self.assertIsNone(g.seed_from_db(Broken()))


if __name__ == "__main__":
    unittest.main()
