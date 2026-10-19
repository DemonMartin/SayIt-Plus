"""网页体验并发名额：每条结束路径都要按占用时的 key 归还。

经过反向代理时，连接对端是代理地址，占名额用的是 X-Forwarded-For 里的访客 IP。归还时如果拿错
key，名额就一直占着，同一 IP 用满并发数之后一直被拒。这里让对端是一个「代理」地址、带上 XFF，
每条结束路径都连着跑超过并发数的次数，跑完名额表必须是空的。
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.config import GuardConfig
from backend.app.guard import Guard

VISITOR = "198.51.100.23"
PROXY = "10.0.0.5"
LIMIT = 2


class FakeASR:
    _vad = None

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, audio, context=None, language=None):
        self.calls += 1
        return "识别结果", 5, {"context": ""}


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


class WebDemoSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.asr = FakeASR()
        telemetry = MagicMock()
        telemetry.create_session.return_value = "sid-test"
        for p in (
            patch.object(main, "asr_engine", self.asr),
            patch.object(main, "telemetry_service", telemetry),
            patch.object(main, "guard", Guard(GuardConfig())),  # 没有 guard 段：今天的行为
            patch.object(main.cfg.web_demo, "enabled", True),
            patch.object(main.cfg.web_demo, "max_concurrency_per_ip", LIMIT),
        ):
            p.start()
            self.addCleanup(p.stop)
        main._web_demo_active_by_ip.clear()
        self.addCleanup(main._web_demo_active_by_ip.clear)
        self.client = TestClient(main.app, client=(PROXY, 50000))
        self.headers = {"x-forwarded-for": VISITOR}

    def _connect(self):
        return self.client.websocket_connect("/ws/transcribe?client=web_demo", headers=self.headers)

    def _session(self, audio: bytes | None) -> list[dict]:
        with self._connect() as ws:
            ws.receive_json()
            ws.send_json({"cmd": "start"})
            if audio:
                ws.send_bytes(audio)
            ws.send_json({"cmd": "stop"})
            return _until_done(ws)

    def _assert_slot_comes_back(self, run) -> None:
        for i in range(LIMIT + 2):
            msgs = run()
            errors = [m.get("message", "") for m in msgs if m.get("type") == "error"]
            self.assertFalse(any("同时发起" in e for e in errors), f"第 {i + 1} 次被并发上限拒了：{errors}")
            self.assertEqual(dict(main._web_demo_active_by_ip), {}, f"第 {i + 1} 次结束后名额没还")

    def test_finished_session(self) -> None:
        self._assert_slot_comes_back(lambda: self._session(_tone(1.5)))
        self.assertEqual(self.asr.calls, LIMIT + 2)

    def test_no_audio(self) -> None:
        self._assert_slot_comes_back(lambda: self._session(None))

    def test_too_short(self) -> None:
        self._assert_slot_comes_back(lambda: self._session(_tone(0.1)))

    def test_silent(self) -> None:
        self._assert_slot_comes_back(lambda: self._session(np.zeros(16000, dtype=np.int16).tobytes()))

    def test_too_long(self) -> None:
        with patch.object(main.cfg.web_demo, "max_duration_sec", 1):
            self._assert_slot_comes_back(lambda: self._session(_tone(1.5)))
        self.assertEqual(self.asr.calls, 0)

    def test_pipeline_exception(self) -> None:
        with patch.object(main, "_run_asr_llm", AsyncMock(side_effect=RuntimeError("boom"))):
            self._assert_slot_comes_back(lambda: self._session(_tone(1.5)))

    def test_pipeline_error(self) -> None:
        with patch.object(main, "_run_asr_llm", AsyncMock(return_value={"error": "boom"})):
            self._assert_slot_comes_back(lambda: self._session(_tone(1.5)))

    def test_concurrency_cap_still_applies(self) -> None:
        with self._connect() as a, self._connect() as b:
            for ws in (a, b):
                ws.receive_json()
                ws.send_json({"cmd": "start"})
                ws.send_json({"cmd": "ping"})  # 等到 pong，说明前面的 start 已经处理完、名额已占上
                self.assertEqual(ws.receive_json()["type"], "pong")
            with self._connect() as c:
                c.receive_json()
                c.send_json({"cmd": "start"})
                rejected = _until_done(c)
        self.assertEqual(rejected[0]["type"], "error")
        self.assertIn("同时发起", rejected[0]["message"])
        # 连接断开时 finally 里也按同一个 key 还
        self.assertEqual(dict(main._web_demo_active_by_ip), {})
        self.assertEqual(len(self._session(_tone(1.5))), 3)


if __name__ == "__main__":
    unittest.main()
