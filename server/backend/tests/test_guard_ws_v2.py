"""guard 第二版接进 /ws/transcribe 之后的端到端行为，以及会话次数的启动补账。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import main
from backend.app.config import GuardAdmissionConfig, GuardConfig, GuardQuotaConfig, GuardWebDemoConfig
from backend.app.db import Database
from backend.app.guard import GENERIC_REJECT_MESSAGE, Guard
from backend.app.quota import RATE_DEVICE_1H, RATE_DEVICE_24H, RATE_EMPTY_DEVICE_1H, RATE_IP_1H

GOOD_DEVICE = "sayit-0f1e2d3c-4b5a-4968-8778-a1b2c3d4e5f6"
GOOD_META = {"device_id": GOOD_DEVICE, "client_version": "9.8.7"}
SITE = "https://site.example"


class FakeASR:
    _vad = None

    def __init__(self, text: str = "这是一段测试识别结果") -> None:
        self.calls = 0
        self.text = text

    async def transcribe(self, audio, context=None, language=None):
        self.calls += 1
        return self.text, 5, {"context": ""}


def _tone(seconds: float) -> bytes:
    t = np.arange(int(seconds * 16000), dtype=np.float32) / 16000.0
    return (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16).tobytes()


def _until_done(ws) -> list[dict]:
    msgs = []
    while True:
        msg = ws.receive_json()
        msgs.append(msg)
        if msg.get("type") == "done":
            return msgs


class GuardV2WebSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.asr = FakeASR()
        telemetry = MagicMock()
        telemetry.create_session.return_value = "sid-test"
        for p in (
            patch.object(main, "asr_engine", self.asr),
            patch.object(main, "telemetry_service", telemetry),
            patch.object(main.cfg.web_demo, "enabled", True),
        ):
            p.start()
            self.addCleanup(p.stop)
        main._web_demo_active_by_ip.clear()
        self.addCleanup(main._web_demo_active_by_ip.clear)

    def _use_guard(self, **kwargs) -> Guard:
        g = Guard(GuardConfig(present=True, trusted_proxies=["127.0.0.0/8"], use_real_ip=True, **kwargs))
        p = patch.object(main, "guard", g)
        p.start()
        self.addCleanup(p.stop)
        return g

    def _client(self) -> TestClient:
        return TestClient(main.app, client=("127.0.0.1", 50000))

    def _session(self, ws, *, source: str = "live", meta=GOOD_META, web_demo: bool = False) -> list[dict]:
        start = {"cmd": "start"} if web_demo else {"cmd": "start", "source": source, "client_meta": meta}
        ws.send_json(start)
        ws.send_bytes(_tone(1.5))
        stop = {"cmd": "stop"} if web_demo else {"cmd": "stop", "usage_meta": {"ptt_hold_ms": 1500}}
        ws.send_json(stop)
        return _until_done(ws)

    def test_web_demo_without_browser_origin_is_closed(self) -> None:
        self._use_guard(web_demo=GuardWebDemoConfig(mode="enforce", allowed_origins=[SITE]))
        with self._client().websocket_connect("/ws/transcribe?client=web_demo") as ws:
            error = ws.receive_json()
            done = ws.receive_json()
            with self.assertRaises(WebSocketDisconnect) as cm:
                ws.receive_json()
        self.assertEqual(error["message"], GENERIC_REJECT_MESSAGE)
        self.assertEqual(done, {"type": "done"})
        self.assertEqual(cm.exception.code, 4003)
        self.assertEqual(main._ws_by_ip.get("127.0.0.1"), None)  # 被拒的连接不占计数

    def test_web_demo_with_site_origin_works(self) -> None:
        self._use_guard(web_demo=GuardWebDemoConfig(mode="enforce", allowed_origins=[SITE]))
        with self._client().websocket_connect("/ws/transcribe?client=web_demo", headers={"origin": SITE}) as ws:
            self.assertEqual(ws.receive_json()["type"], "ready")
            msgs = self._session(ws, web_demo=True)
        self.assertEqual([m["type"] for m in msgs], ["asr", "final", "done"])

    def test_desktop_custom_source_is_closed(self) -> None:
        self._use_guard(admission=GuardAdmissionConfig(mode="enforce"))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            ws.send_json({"cmd": "start", "source": "sailchat_blender_wake", "client_meta": GOOD_META})
            error = ws.receive_json()
            ws.receive_json()
            with self.assertRaises(WebSocketDisconnect) as cm:
                ws.receive_json()
        self.assertEqual(error["message"], GENERIC_REJECT_MESSAGE)
        self.assertEqual(cm.exception.code, 4003)
        self.assertEqual(self.asr.calls, 0)

    def test_empty_results_throttle_the_device(self) -> None:
        self.asr.text = "嗯。"
        g = self._use_guard(quota=GuardQuotaConfig(mode="enforce", device_empty_per_hour=2))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            first = self._session(ws)
            second = self._session(ws)
            ws.send_json({"cmd": "start", "source": "live", "client_meta": GOOD_META})
            third = _until_done(ws)
            ws.send_json({"cmd": "ping"})
            pong = ws.receive_json()
        self.assertEqual([m["type"] for m in first], ["asr", "final", "done"])
        self.assertEqual([m["type"] for m in second], ["asr", "final", "done"])
        self.assertEqual([m["type"] for m in third], ["error", "done"])
        self.assertIn("rate limit", third[0]["message"])
        self.assertEqual(pong["type"], "pong")  # 连接没断
        self.assertEqual(self.asr.calls, 2)
        self.assertEqual(g.ledger.rate[RATE_EMPTY_DEVICE_1H].total(GOOD_DEVICE, g.now_ms()), 2)

    def test_real_text_does_not_count_as_empty(self) -> None:
        g = self._use_guard(quota=GuardQuotaConfig(mode="enforce", device_empty_per_hour=1))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            for _ in range(3):
                self.assertEqual([m["type"] for m in self._session(ws)], ["asr", "final", "done"])
        self.assertEqual(g.ledger.rate[RATE_EMPTY_DEVICE_1H].total(GOOD_DEVICE, g.now_ms()), 0)

    def test_web_demo_hourly_cap_keeps_connection_and_slot_free(self) -> None:
        self._use_guard(quota=GuardQuotaConfig(mode="enforce", demo_ip_sessions_per_hour=2))
        headers = {"x-forwarded-for": "198.51.100.23"}
        with self._client().websocket_connect("/ws/transcribe?client=web_demo", headers=headers) as ws:
            ws.receive_json()
            a = self._session(ws, web_demo=True)
            b = self._session(ws, web_demo=True)
            ws.send_json({"cmd": "start"})
            c = _until_done(ws)
            slots = dict(main._web_demo_active_by_ip)
        self.assertEqual([m["type"] for m in a], ["asr", "final", "done"])
        self.assertEqual([m["type"] for m in b], ["asr", "final", "done"])
        self.assertEqual(c[0]["type"], "error")
        self.assertIn("网页体验次数已用完", c[0]["message"])
        self.assertEqual(slots, {})

    def test_no_guard_section_ignores_all_new_rules(self) -> None:
        g = Guard(GuardConfig())
        self.asr.text = "嗯。"
        with patch.object(main, "guard", g):
            with self._client().websocket_connect("/ws/transcribe?client=web_demo") as ws:  # 没有 Origin
                ws.receive_json()
                for _ in range(3):
                    self.assertEqual([m["type"] for m in self._session(ws, web_demo=True)], ["asr", "final", "done"])
            with self._client().websocket_connect("/ws/transcribe") as ws:
                ws.receive_json()
                msgs = self._session(ws, source="sailchat_blender_wake", meta={})
        self.assertEqual([m["type"] for m in msgs], ["asr", "final", "done"])


class RateSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="sayit-guard-rate-seed-"))
        self.db = Database(str(self._tmp / "t.sqlite3"))
        self.db.initialize()
        self.now = 1_800_000_000.0

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _row(self, sid: str, ago_ms: int, status: str, *, device: str | None = GOOD_DEVICE) -> None:
        started = int(self.now * 1000) - ago_ms
        self.db.execute(
            "INSERT INTO usage_sessions(session_id, connection_id, started_at, device_id, client_ip, forwarded_for,"
            " source, audio_duration_ms, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, "cid", started, device, "127.0.0.1", "198.51.100.7", "live", 1000, status, started, started),
        )

    def test_session_counts_survive_a_restart(self) -> None:
        self._row("a", 10 * 60_000, "success")
        self._row("b", 20 * 60_000, "empty_result")
        self._row("c", 30 * 60_000, "empty_audio")          # 没识别的也算一次会话
        self._row("d", 5 * 3600_000, "success")             # 一小时外、24 小时内
        self._row("e", 30 * 3600_000, "success")            # 24 小时外
        self._row("demo", 5 * 60_000, "success", device=None)  # 没有 device_id：不补
        g = Guard(
            GuardConfig(present=True, trusted_proxies=["127.0.0.0/8"], use_real_ip=True,
                        quota=GuardQuotaConfig(mode="enforce", device_sessions_per_hour=3)),
            clock=lambda: self.now,
        )
        g.seed_from_db(self.db)
        now = g.now_ms()
        self.assertEqual(g.ledger.rate[RATE_DEVICE_1H].total(GOOD_DEVICE, now), 3)
        self.assertEqual(g.ledger.rate[RATE_DEVICE_24H].total(GOOD_DEVICE, now), 4)
        self.assertEqual(g.ledger.rate[RATE_EMPTY_DEVICE_1H].total(GOOD_DEVICE, now), 1)
        self.assertEqual(g.ledger.rate[RATE_IP_1H].total("198.51.100.7", now), 3)
        self.assertIn("seeded_rate=4/5", g.banner_line())
        conn = g.connection(cid="c", peer="127.0.0.1", forwarded="198.51.100.7", xff="198.51.100.7", is_web_demo=False)
        self.assertIsNotNone(conn.check_start(source="live", client_meta=GOOD_META, compare=False, sid="s"))


if __name__ == "__main__":
    unittest.main()
