"""guard 接进 /ws/transcribe 之后的端到端行为。

不跑 lifespan（不加载 ASR / LLM），ASR 与 LLM 换成计数的假实现；每个用例换一个 Guard。
重点钉住两件事：没有 guard 段时行为与改动前完全一样；每条拒绝路径上 ASR 一次都没被调用。
"""
from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import main
from backend.app.config import (
    GuardAdmissionConfig,
    GuardAiBudgetConfig,
    GuardConfig,
    GuardPaceConfig,
    GuardQuotaConfig,
)
from backend.app.guard import GENERIC_REJECT_MESSAGE, Guard

GOOD_DEVICE = "sayit-0f1e2d3c-4b5a-4968-8778-a1b2c3d4e5f6"
GOOD_META = {"device_id": GOOD_DEVICE, "client_version": "9.8.7"}
ASR_TEXT = "这是一段测试识别结果"


class FakeASR:
    _vad = None

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, audio, context=None, language=None):
        self.calls += 1
        return ASR_TEXT, 5, {"context": ""}


class FakeLLM:
    def __init__(self, usage: dict | None = None) -> None:
        self.calls = 0
        self.usage = usage

    async def polish(self, text, system_prompt=None, text_context=None, is_web_demo=False):
        self.calls += 1
        debug = {"provider": "fake", "model": "fake-model"}
        if self.usage is not None:
            debug["usage"] = dict(self.usage)
        return text + "。", 7, debug


def _tone(seconds: float) -> bytes:
    t = np.arange(int(seconds * 16000), dtype=np.float32) / 16000.0
    return (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16).tobytes()


def _chunks(seconds: float) -> list[bytes]:
    pcm = _tone(seconds)
    return [pcm[i:i + 32000] for i in range(0, len(pcm), 32000)]


class GuardWebSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.asr = FakeASR()
        self.telemetry = MagicMock()
        self.telemetry.create_session.return_value = "sid-test"
        self._patches = [
            patch.object(main, "asr_engine", self.asr),
            patch.object(main, "telemetry_service", self.telemetry),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self._patches)])

    def _use_guard(self, **kwargs) -> Guard:
        g = Guard(GuardConfig(**kwargs))
        p = patch.object(main, "guard", g)
        p.start()
        self.addCleanup(p.stop)
        return g

    def _client(self, peer: str = "127.0.0.1") -> TestClient:
        return TestClient(main.app, client=(peer, 50000))

    @staticmethod
    def _until_done(ws) -> list[dict]:
        msgs = []
        while True:
            msg = ws.receive_json()
            msgs.append(msg)
            if msg.get("type") == "done":
                return msgs

    def _session(self, ws, *, seconds: float = 10.0, meta=GOOD_META, source: str = "live", ptt: bool = True) -> list[dict]:
        ws.send_json({"cmd": "start", "source": source, "client_meta": meta})
        for chunk in _chunks(seconds):
            ws.send_bytes(chunk)
        stop = {"cmd": "stop"}
        if ptt:
            stop["usage_meta"] = {"ptt_hold_ms": int(seconds * 1000)}
        ws.send_json(stop)
        return self._until_done(ws)

    def _terminal_statuses(self) -> list[str]:
        # 真实的 TelemetryService 对 session_id=None 什么都不做，这里也只看带会话的调用
        return [c.args[2] for c in self.telemetry.mark_terminal_status.call_args_list if c.args[0] is not None]

    # ── 没有 guard 段 = 今天的行为 ─────────────────────────────────────

    def test_missing_section_behaves_exactly_as_before(self) -> None:
        self._use_guard()  # GuardConfig() = 没有 guard 段
        headers = {"x-forwarded-for": "198.51.100.1, 203.0.113.9"}
        with self._client().websocket_connect("/ws/transcribe", headers=headers) as ws:
            ready = ws.receive_json()
            self.assertEqual(ready["type"], "ready")
            # 旧取法：XFF 最左一个
            self.assertIn("198.51.100.1", main._ws_by_ip)
            self.assertEqual(main._client_ip_key("127.0.0.1", "198.51.100.1, 203.0.113.9"), "198.51.100.1")
            # 瞬间推完 10 秒、device_id 乱写、stop 不带 ptt：照样识别
            msgs = self._session(ws, meta={"device_id": "whatever"}, ptt=False)
        types = [m["type"] for m in msgs]
        self.assertEqual(types, ["asr", "final", "done"])
        final = msgs[1]
        self.assertNotIn("llm_skipped", final)
        self.assertEqual(final["asr_text"], ASR_TEXT)
        self.assertEqual(self.asr.calls, 1)
        self.assertEqual(self._terminal_statuses(), [])
        self.assertTrue(main.guard.banner_line().startswith("guard banner configured=no"))

    # ── 推流检测 ──────────────────────────────────────────────────────

    def test_pace_enforce_rejects_instant_push(self) -> None:
        self._use_guard(present=True, pace=GuardPaceConfig(mode="enforce", max_ratio=1.5, grace_sec=3))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            ws.send_json({"cmd": "start", "source": "live", "client_meta": GOOD_META})
            for chunk in _chunks(10):
                ws.send_bytes(chunk)
            error = ws.receive_json()
            done = ws.receive_json()
            with self.assertRaises(WebSocketDisconnect) as cm:
                ws.receive_json()
        self.assertEqual(error, {"type": "error", "code": "rejected", "message": GENERIC_REJECT_MESSAGE})
        self.assertEqual(done, {"type": "done"})
        self.assertEqual(cm.exception.code, main._WS_CLOSE_REJECTED)
        self.assertEqual(main._WS_CLOSE_REJECTED, 4003)
        self.assertEqual(self.asr.calls, 0)
        self.assertEqual(self._terminal_statuses(), ["rejected_pace"])

    def test_pace_log_mode_lets_session_through(self) -> None:
        self._use_guard(present=True, pace=GuardPaceConfig(mode="log", max_ratio=1.5, grace_sec=3))
        with self.assertLogs("sayit.guard", level="INFO") as logs:
            with self._client().websocket_connect("/ws/transcribe") as ws:
                ws.receive_json()
                msgs = self._session(ws)
        self.assertEqual([m["type"] for m in msgs], ["asr", "final", "done"])
        self.assertEqual(self.asr.calls, 1)
        self.assertEqual(sum("guard violation dim=pace" in line for line in logs.output), 1)

    def test_web_demo_replay_is_not_pace_checked(self) -> None:
        self._use_guard(present=True, pace=GuardPaceConfig(mode="enforce", max_ratio=1.5, grace_sec=3),
                        admission=GuardAdmissionConfig(mode="enforce"))
        with self._client().websocket_connect("/ws/transcribe?client=web_demo") as ws:
            ws.receive_json()
            ws.send_json({"cmd": "start"})
            for chunk in _chunks(10):
                ws.send_bytes(chunk)
            ws.send_json({"cmd": "stop"})
            msgs = self._until_done(ws)
        self.assertEqual([m["type"] for m in msgs], ["asr", "final", "done"])
        self.assertEqual(self.asr.calls, 1)

    # ── 准入 ──────────────────────────────────────────────────────────

    def test_admission_enforce_rejects_bad_device_at_start(self) -> None:
        self._use_guard(present=True, admission=GuardAdmissionConfig(mode="enforce"))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            ws.send_json({"cmd": "start", "source": "live", "client_meta": {"device_id": "x", "client_version": "1.2.3"}})
            error = ws.receive_json()
            done = ws.receive_json()
            with self.assertRaises(WebSocketDisconnect) as cm:
                ws.receive_json()
        self.assertEqual(error["code"], "rejected")
        self.assertEqual(error["message"], GENERIC_REJECT_MESSAGE)
        self.assertEqual(done["type"], "done")
        self.assertEqual(cm.exception.code, 4003)
        self.assertEqual(self.asr.calls, 0)
        # start 时被拒不建 telemetry 会话：脚本反复发 start 不会放大写库
        self.telemetry.create_session.assert_not_called()
        self.assertEqual(self._terminal_statuses(), [])

    def test_admission_enforce_rejects_live_stop_without_ptt(self) -> None:
        self._use_guard(present=True, admission=GuardAdmissionConfig(mode="enforce"))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            msgs = self._session(ws, seconds=2, ptt=False)
            with self.assertRaises(WebSocketDisconnect) as cm:
                ws.receive_json()
        self.assertEqual([m["type"] for m in msgs], ["error", "done"])
        self.assertEqual(msgs[0]["message"], GENERIC_REJECT_MESSAGE)
        self.assertEqual(cm.exception.code, 4003)
        self.assertEqual(self.asr.calls, 0)
        self.assertEqual(self._terminal_statuses(), ["rejected_admission"])

    def test_admission_passes_real_client_and_history_reprocess(self) -> None:
        self._use_guard(present=True, admission=GuardAdmissionConfig(mode="enforce"),
                        pace=GuardPaceConfig(mode="enforce", max_ratio=1.5, grace_sec=3))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            # 历史记录重新识别：一次性推完、stop 不带 ptt，都是正常的
            msgs = self._session(ws, seconds=10, source="history_reprocess", ptt=False)
        self.assertEqual([m["type"] for m in msgs], ["asr", "final", "done"])
        self.assertEqual(self.asr.calls, 1)

    # ── 音频额度 ──────────────────────────────────────────────────────

    def test_quota_enforce_rejects_next_start_without_closing(self) -> None:
        self._use_guard(present=True, quota=GuardQuotaConfig(mode="enforce", device_minutes=0.02))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            first = self._session(ws, seconds=1.5)
            ws.send_json({"cmd": "start", "source": "live", "client_meta": GOOD_META})
            second = self._until_done(ws)
            # 被拒之后客户端照样推音频、发 stop：音频被丢掉，只回 done，不进识别
            for chunk in _chunks(1.5):
                ws.send_bytes(chunk)
            ws.send_json({"cmd": "stop", "usage_meta": {"ptt_hold_ms": 1500}})
            after = self._until_done(ws)
            # 连接没断：还能 ping
            ws.send_json({"cmd": "ping"})
            pong = ws.receive_json()
        self.assertEqual([m["type"] for m in first], ["asr", "final", "done"])
        self.assertEqual([m["type"] for m in second], ["error", "done"])
        self.assertEqual(second[0]["code"], "quota")
        self.assertIn("quota", second[0]["message"])
        self.assertRegex(second[0]["message"], r"约 \d+ (分钟|小时)后恢复$")
        self.assertEqual(after, [{"type": "done"}])
        self.assertEqual(pong["type"], "pong")
        self.assertEqual(self.asr.calls, 1)
        # 第二次 start 被拒：不建会话，所以也没有终态可记
        self.assertEqual(self.telemetry.create_session.call_count, 1)
        self.assertEqual(self._terminal_statuses(), [])

    # ── 润色预算 ──────────────────────────────────────────────────────

    def test_ai_budget_enforce_skips_llm_only(self) -> None:
        llm = FakeLLM()
        for p in (patch.object(main, "llm_engine", llm), patch.object(main.cfg.llm, "enabled", True)):
            p.start()
            self.addCleanup(p.stop)
        self._use_guard(present=True, ai_budget=GuardAiBudgetConfig(mode="enforce", device_tokens=5))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            first = self._session(ws, seconds=1.5)
            second = self._session(ws, seconds=1.5)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(self.asr.calls, 2)
        final1 = next(m for m in first if m["type"] == "final")
        final2 = next(m for m in second if m["type"] == "final")
        self.assertEqual(final1["llm_text"], ASR_TEXT + "。")
        self.assertNotIn("llm_skipped", final1)
        self.assertEqual(final2["llm_skipped"], "quota")
        self.assertEqual(final2["llm_text"], final2["asr_text"])
        self.assertEqual(self._terminal_statuses(), [])

    # ── 可信 IP 与白名单 ──────────────────────────────────────────────

    def test_use_real_ip_switches_the_connection_key(self) -> None:
        headers = {"x-forwarded-for": "198.51.100.1, 203.0.113.9"}
        for use_real_ip, expected in ((False, "198.51.100.1"), (True, "203.0.113.9")):
            with self.subTest(use_real_ip=use_real_ip):
                g = Guard(GuardConfig(present=True, trusted_proxies=["127.0.0.0/8"], use_real_ip=use_real_ip))
                with patch.object(main, "guard", g):
                    with self._client().websocket_connect("/ws/transcribe", headers=headers) as ws:
                        ws.receive_json()
                        self.assertIn(expected, main._ws_by_ip)
                self.assertNotIn(expected, main._ws_by_ip)

    def test_whitelisted_ip_skips_pace_and_admission(self) -> None:
        self._use_guard(
            present=True,
            trusted_proxies=["127.0.0.0/8"],
            allow_ips=["203.0.113.9"],
            pace=GuardPaceConfig(mode="enforce", max_ratio=1.5, grace_sec=3),
            admission=GuardAdmissionConfig(mode="enforce"),
        )
        headers = {"x-forwarded-for": "203.0.113.9"}
        with self._client().websocket_connect("/ws/transcribe", headers=headers) as ws:
            ws.receive_json()
            msgs = self._session(ws, seconds=10, meta={}, ptt=False)
        self.assertEqual([m["type"] for m in msgs], ["asr", "final", "done"])
        self.assertEqual(self.asr.calls, 1)

    # ── 推流按连接累计 ────────────────────────────────────────────────

    def test_pace_budget_is_per_connection(self) -> None:
        # 每段都短于余量，单看一段永远不超；连起来看是瞬推，第二段就该被拦
        self._use_guard(present=True, pace=GuardPaceConfig(mode="enforce", max_ratio=1.5, grace_sec=3))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            first = self._session(ws, seconds=2.5)
            ws.send_json({"cmd": "start", "source": "live", "client_meta": GOOD_META})
            for chunk in _chunks(2.5):
                ws.send_bytes(chunk)
            error = ws.receive_json()
            done = ws.receive_json()
            with self.assertRaises(WebSocketDisconnect) as cm:
                ws.receive_json()
        self.assertEqual([m["type"] for m in first], ["asr", "final", "done"])
        self.assertEqual(error["message"], GENERIC_REJECT_MESSAGE)
        self.assertEqual(done, {"type": "done"})
        self.assertEqual(cm.exception.code, 4003)
        self.assertEqual(self.asr.calls, 1)

    # ── resume 不能绕过 start ────────────────────────────────────────

    def test_resume_without_start_is_rejected_in_enforce(self) -> None:
        self._use_guard(present=True, admission=GuardAdmissionConfig(mode="enforce"))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            ws.send_json({"cmd": "resume"})
            error = ws.receive_json()
            done = ws.receive_json()
            with self.assertRaises(WebSocketDisconnect) as cm:
                ws.receive_json()
        self.assertEqual(error["message"], GENERIC_REJECT_MESSAGE)
        self.assertEqual(done, {"type": "done"})
        self.assertEqual(cm.exception.code, 4003)
        self.assertEqual(self.asr.calls, 0)
        self.telemetry.create_session.assert_not_called()

    def test_resume_after_finished_session_is_rejected(self) -> None:
        self._use_guard(present=True, admission=GuardAdmissionConfig(mode="enforce"))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            first = self._session(ws, seconds=1.5)
            ws.send_json({"cmd": "resume"})
            error = ws.receive_json()
            done = ws.receive_json()
            with self.assertRaises(WebSocketDisconnect) as cm:
                ws.receive_json()
        self.assertEqual([m["type"] for m in first], ["asr", "final", "done"])
        self.assertEqual(error["message"], GENERIC_REJECT_MESSAGE)
        self.assertEqual(done, {"type": "done"})
        self.assertEqual(cm.exception.code, 4003)
        self.assertEqual(self.asr.calls, 1)

    def test_resume_after_quota_rejection_is_still_quota_checked(self) -> None:
        # 准入没开时，resume 补出来的隐式会话沿用上一个会话的设备，按设备的额度照样拦
        self._use_guard(present=True, quota=GuardQuotaConfig(mode="enforce", device_minutes=0.02))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            first = self._session(ws, seconds=1.5)
            ws.send_json({"cmd": "start", "source": "live", "client_meta": GOOD_META})
            rejected = self._until_done(ws)
            ws.send_json({"cmd": "resume"})
            resumed = self._until_done(ws)
            for chunk in _chunks(1.5):
                ws.send_bytes(chunk)
            ws.send_json({"cmd": "stop", "usage_meta": {"ptt_hold_ms": 1500}})
            after = self._until_done(ws)
        self.assertEqual([m["type"] for m in first], ["asr", "final", "done"])
        self.assertEqual(rejected[0]["code"], "quota")
        self.assertEqual(resumed[0]["code"], "quota")
        self.assertEqual(after, [{"type": "done"}])
        self.assertEqual(self.asr.calls, 1)

    def test_resume_without_start_in_log_mode_is_logged_and_charged(self) -> None:
        g = self._use_guard(present=True, admission=GuardAdmissionConfig(mode="log"),
                            pace=GuardPaceConfig(mode="log", max_ratio=1.5, grace_sec=3))
        with self.assertLogs("sayit.guard", level="INFO") as logs:
            with self._client().websocket_connect("/ws/transcribe") as ws:
                ws.receive_json()
                ws.send_json({"cmd": "resume"})
                for chunk in _chunks(10):
                    ws.send_bytes(chunk)
                ws.send_json({"cmd": "stop"})
                msgs = self._until_done(ws)
        self.assertEqual([m["type"] for m in msgs], ["asr", "final", "done"])
        self.assertTrue(any("guard violation dim=admission.no_start" in line for line in logs.output))
        self.assertTrue(any("guard violation dim=pace" in line for line in logs.output))
        self.assertEqual(g.ledger.audio["ip"].total("127.0.0.1", g.now_ms()), 10_000)

    def test_pause_and_resume_inside_a_session_still_work(self) -> None:
        self._use_guard(present=True, admission=GuardAdmissionConfig(mode="enforce"))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            ws.send_json({"cmd": "start", "source": "live", "client_meta": GOOD_META})
            ws.send_bytes(_tone(0.5))
            ws.send_json({"cmd": "pause"})
            ws.send_json({"cmd": "resume"})
            ws.send_bytes(_tone(1.0))
            ws.send_json({"cmd": "stop", "usage_meta": {"ptt_hold_ms": 1500}})
            msgs = self._until_done(ws)
        self.assertEqual([m["type"] for m in msgs], ["asr", "final", "done"])
        self.assertEqual(self.asr.calls, 1)

    # ── 记账时机 ──────────────────────────────────────────────────────

    def test_audio_is_charged_before_the_pipeline_runs(self) -> None:
        # 客户端发完 stop 立刻断开时，识别结果发不回去、识别那一步抛异常，后面的代码走不到。
        # 音频必须在送进识别之前就记上。
        g = self._use_guard(present=True, quota=GuardQuotaConfig(mode="log", device_minutes=100))
        broken = AsyncMock(side_effect=RuntimeError("client went away mid-pipeline"))
        with patch.object(main, "_run_asr_llm", broken):
            with self._client().websocket_connect("/ws/transcribe") as ws:
                ws.receive_json()
                msgs = self._session(ws, seconds=2)
        self.assertEqual([m["type"] for m in msgs], ["error", "done"])
        broken.assert_awaited_once()
        self.assertEqual(g.ledger.audio["device"].total(GOOD_DEVICE, g.now_ms()), 2000)

    def _use_llm(self, llm) -> None:
        for p in (patch.object(main, "llm_engine", llm), patch.object(main.cfg.llm, "enabled", True)):
            p.start()
            self.addCleanup(p.stop)

    def _polish_session(self, ws, *, system_prompt=None, text_context=None) -> list[dict]:
        start = {"cmd": "start", "source": "live", "client_meta": GOOD_META}
        if system_prompt is not None:
            start["system_prompt"] = system_prompt
        if text_context is not None:
            start["text_context"] = text_context
        ws.send_json(start)
        for chunk in _chunks(1.5):
            ws.send_bytes(chunk)
        ws.send_json({"cmd": "stop", "usage_meta": {"ptt_hold_ms": 1500}})
        return self._until_done(ws)

    def test_reported_tokens_are_what_gets_charged(self) -> None:
        llm = FakeLLM(usage={"input_tokens": 1234, "output_tokens": 56, "source": "api"})
        self._use_llm(llm)
        g = self._use_guard(present=True, ai_budget=GuardAiBudgetConfig(mode="log", device_tokens=10**9))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            self._polish_session(ws, system_prompt="提" * 5000)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(g.ledger.ai["device"].total(GOOD_DEVICE, g.now_ms()), 1234 + 56)

    def test_without_reported_usage_everything_sent_is_estimated(self) -> None:
        llm = FakeLLM()  # 不报用量：按送进去的字数估
        self._use_llm(llm)
        g = self._use_guard(present=True, ai_budget=GuardAiBudgetConfig(mode="log", device_tokens=10**9))
        prompt = "提" * 700
        ctx = {"text_before": "前" * 200, "selected_text": "选" * 300, "text_after": "后" * 100}
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            msgs = self._polish_session(ws, system_prompt=prompt, text_context=ctx)
        final = next(m for m in msgs if m["type"] == "final")
        expected = len(prompt) + len(ASR_TEXT) + 200 + 300 + 100 + len(final["llm_text"])
        self.assertEqual(g.ledger.ai["device"].total(GOOD_DEVICE, g.now_ms()), expected)

    def test_oversized_system_prompt_skips_polishing_in_enforce(self) -> None:
        llm = FakeLLM()
        self._use_llm(llm)
        self._use_guard(present=True, ai_budget=GuardAiBudgetConfig(mode="enforce", max_system_prompt_chars=1000))
        with self.assertLogs("sayit", level="INFO") as logs:
            with self._client().websocket_connect("/ws/transcribe") as ws:
                ws.receive_json()
                normal = self._polish_session(ws, system_prompt="提" * 1000)
                oversized = self._polish_session(ws, system_prompt="提" * 5000)
        self.assertEqual(llm.calls, 1)  # 只有正常长度那次调了模型
        self.assertNotIn("llm_skipped", next(m for m in normal if m["type"] == "final"))
        final = next(m for m in oversized if m["type"] == "final")
        self.assertEqual(final["llm_skipped"], "quota")
        self.assertEqual(final["llm_text"], final["asr_text"])  # 识别照常返回
        self.assertEqual(self.asr.calls, 2)
        self.assertTrue(any("guard degrade dim=ai.prompt" in line and "prompt=5000" in line for line in logs.output))
        self.assertTrue(any("prompt_len=5000" in line for line in logs.output))  # 会话开始就记了长度

    def test_non_string_system_prompt_is_measured_too(self) -> None:
        llm = FakeLLM()
        self._use_llm(llm)
        self._use_guard(present=True, ai_budget=GuardAiBudgetConfig(mode="enforce", max_system_prompt_chars=1000))
        with self._client().websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            msgs = self._polish_session(ws, system_prompt=[{"type": "text", "text": "提" * 5000}])
        self.assertEqual(llm.calls, 0)
        self.assertEqual(next(m for m in msgs if m["type"] == "final")["llm_skipped"], "quota")

    # ── 网页体验 ──────────────────────────────────────────────────────

    def test_web_demo_quota_rejection_releases_the_concurrency_slot(self) -> None:
        g = self._use_guard(present=True, trusted_proxies=["127.0.0.0/8"], use_real_ip=True,
                            quota=GuardQuotaConfig(mode="enforce", demo_ip_minutes=0.5))
        g.ledger.add(g.ledger.audio, [("demo_ip", "203.0.113.7")], 30_000, g.now_ms())
        headers = {"x-forwarded-for": "203.0.113.7"}
        with self._client().websocket_connect("/ws/transcribe?client=web_demo", headers=headers) as ws:
            ws.receive_json()
            ws.send_json({"cmd": "start"})
            msgs = self._until_done(ws)
            slots = main._web_demo_active_by_ip.get("203.0.113.7", 0)
        self.assertEqual([m["type"] for m in msgs], ["error", "done"])
        self.assertEqual(msgs[0]["code"], "quota")
        self.assertEqual(slots, 0)
        self.assertEqual(self.asr.calls, 0)


class FeedbackIpTests(unittest.TestCase):
    """第 5 个取 IP 的地方：/api/feedback 存库的 client_ip。"""

    def _post(self, resolver) -> str:
        db = MagicMock()
        db.dialect.placeholder = "?"
        with patch.object(main, "database", db), patch.object(main.app.state, "ip_resolver", resolver):
            client = TestClient(main.app, client=("127.0.0.1", 50000))
            resp = client.post(
                "/api/feedback",
                json={"machine_id": f"m-guard-{id(resolver)}", "feedback_text": "测试反馈"},
                headers={"x-forwarded-for": "198.51.100.1, 203.0.113.9"},
            )
        self.assertEqual(resp.status_code, 200)
        return db.execute.call_args.args[1][2]

    def test_trusted_ip_is_stored_when_use_real_ip_is_active(self) -> None:
        resolver = Guard(GuardConfig(present=True, trusted_proxies=["127.0.0.0/8"], use_real_ip=True)).resolver
        self.assertEqual(self._post(resolver), "203.0.113.9")

    def test_old_leftmost_value_without_guard(self) -> None:
        self.assertEqual(self._post(Guard(GuardConfig()).resolver), "198.51.100.1")


class LifespanWiringTests(unittest.TestCase):
    """真跑一遍 _lifespan（引擎、数据库全换成假的）：启动时补账 + 打 banner + 起统计任务，退出时停掉。"""

    def _run(self, g: Guard) -> tuple[MagicMock, list[str]]:
        fake_db = MagicMock()
        fake_db.fetch_all.return_value = []
        fake_asr = MagicMock()
        fake_asr.close = AsyncMock()
        with ExitStack() as stack:
            # lifespan 会改这些模块全局变量，patch 退出时还原，免得影响别的用例
            for name in ("database", "telemetry_service", "asr_engine", "llm_engine", "web_demo_llm_engine"):
                stack.enter_context(patch.object(main, name, None))
            stack.enter_context(patch.object(main, "Database", return_value=fake_db))
            stack.enter_context(patch.object(main, "attach_database_log_handler"))
            stack.enter_context(patch.object(main, "TelemetryService", return_value=MagicMock()))
            stack.enter_context(patch.object(main, "ASREngine", return_value=fake_asr))
            stack.enter_context(patch.object(main, "guard", g))
            logs = stack.enter_context(self.assertLogs("sayit.guard", level="INFO"))
            with TestClient(main.app):
                self.started = g._task is not None
        return fake_db, logs.output

    def test_configured_guard(self) -> None:
        g = Guard(GuardConfig(present=True))
        fake_db, output = self._run(g)
        self.assertTrue(self.started)
        self.assertIsNone(g._task)
        fake_db.fetch_all.assert_called_once()
        banners = [line for line in output if "guard banner" in line]
        self.assertEqual(len(banners), 1)
        self.assertIn("configured=yes", banners[0])
        self.assertIn("seeded=0/0", banners[0])

    def test_missing_section_only_adds_banner(self) -> None:
        g = Guard(GuardConfig())
        fake_db, output = self._run(g)
        self.assertFalse(self.started)
        fake_db.fetch_all.assert_not_called()  # 不查库
        self.assertEqual(len(output), 1)
        self.assertIn("guard banner configured=no", output[0])


if __name__ == "__main__":
    unittest.main()
