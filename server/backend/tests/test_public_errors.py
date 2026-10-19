"""内部报错原文不外发：识别异常、AI 整理失败、诊断上传失败三处。"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from fastapi.testclient import TestClient

from backend.app import diagnostics, main
from backend.app.config import GuardConfig, LLMProfile
from backend.app.guard import Guard
from backend.app.llm import LLMEngine

SECRET = "/srv/sayit/.venv/lib/python3.12/site-packages/vllm/engine.py CUDA out of memory"
REF_RE = re.compile(r"\(ref: ([0-9a-f]{8})\)")


def _tone(seconds: float) -> bytes:
    t = np.arange(int(seconds * 16000), dtype=np.float32) / 16000.0
    return (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16).tobytes()


class PipelineErrorTests(unittest.TestCase):
    def setUp(self):
        telemetry = MagicMock()
        telemetry.create_session.return_value = "sid-test"
        self.telemetry = telemetry
        for p in (
            patch.object(main, "asr_engine", MagicMock()),
            patch.object(main, "telemetry_service", telemetry),
            patch.object(main, "guard", Guard(GuardConfig())),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))

    def _session_messages(self) -> list[dict]:
        with self.client.websocket_connect("/ws/transcribe") as ws:
            ws.receive_json()
            ws.send_json({"cmd": "start"})
            ws.send_bytes(_tone(1.5))
            ws.send_json({"cmd": "stop"})
            msgs = []
            while True:
                m = ws.receive_json()
                msgs.append(m)
                if m.get("type") == "done":
                    return msgs

    def _error(self, exc: BaseException) -> tuple[str, list[str]]:
        with patch.object(main, "_run_asr_llm", AsyncMock(side_effect=exc)), \
             self.assertLogs("sayit.errors", level="ERROR") as logs:
            msgs = self._session_messages()
        errors = [m["message"] for m in msgs if m.get("type") == "error"]
        self.assertEqual(len(errors), 1, msgs)
        return errors[0], logs.output

    def test_generic_exception_is_not_sent_to_client(self):
        message, log_lines = self._error(RuntimeError(SECRET))
        self.assertNotIn("/srv/", message)
        self.assertNotIn("CUDA", message)
        self.assertTrue(message.startswith("Internal server error"), message)
        ref = REF_RE.search(message).group(1)
        # 用编号能在服务端日志里找到原文，日志行里还有 cid 和 sid
        joined = "\n".join(log_lines)
        self.assertIn(f"ref={ref}", joined)
        self.assertIn(SECRET, joined)
        self.assertIn("sid=sid-test", joined)
        self.assertRegex(joined, r"cid=[0-9a-f]{8}")
        # 遥测表照旧有原文，并带上编号便于对照
        detail = self.telemetry.mark_failure.call_args[0][3]
        self.assertIn(ref, detail)
        self.assertIn(SECRET, detail)

    def test_timeout_keeps_timed_out_wording(self):
        message, _ = self._error(asyncio.TimeoutError())
        # 客户端 errorMessages.ts 用 /timeout|timed out/ 判断超时
        self.assertIn("timed out", message)
        self.assertRegex(message, REF_RE)

    def test_httpx_timeout_counts_as_timeout(self):
        import httpx

        message, _ = self._error(httpx.ReadTimeout("read timed out to https://llm-upstream.internal"))
        self.assertIn("timed out", message)
        self.assertNotIn("llm-upstream", message)

    def test_each_error_gets_its_own_ref(self):
        a, _ = self._error(RuntimeError("x"))
        b, _ = self._error(RuntimeError("x"))
        self.assertNotEqual(REF_RE.search(a).group(1), REF_RE.search(b).group(1))


class LlmErrorTests(unittest.TestCase):
    def test_llm_debug_error_is_type_name_only(self):
        engine = LLMEngine.__new__(LLMEngine)
        engine._cfg = LLMProfile(enabled=True, provider="openai")
        engine._build_messages = lambda *a, **k: [{"role": "user", "content": "hi"}]
        engine._estimate_usage = lambda *a, **k: {"total_tokens": 1}

        async def boom(_messages):
            raise RuntimeError("POST https://llm-upstream.example.com/openai/v1 failed: 403")

        engine._call_openai = boom
        with self.assertLogs("sayit", level="ERROR"):
            text, ms, debug = asyncio.run(engine.polish("你好"))
        self.assertEqual(text, "你好")
        self.assertEqual(debug["error"], "RuntimeError")
        self.assertNotIn("llm-upstream", json.dumps(debug))


class DiagnosticsErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(diagnostics, "DIAGNOSTICS_DIR", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        # 每条测试一个新的限流器，免得 5 次/小时的上限串到别的测试
        q = patch.object(diagnostics, "_upload_limiter", diagnostics.KeyedWindowLimiter(window_sec=3600, limit=100))
        q.start()
        self.addCleanup(q.stop)
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))

    def _upload(self, data: bytes):
        return self.client.post("/api/diagnostics", files={"diagnostics": ("diagnostics.zip", data, "application/zip")})

    def _index(self, ticket_id: str) -> dict:
        r = self.client.get(f"/api/diagnostics/{ticket_id}")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_bad_zip_keeps_fixed_message(self):
        r = self._upload(b"not a zip at all")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "Not a valid zip file")
        self.assertEqual(self._index(r.json()["ticket_id"])["error_code"], "bad_zip")

    def test_own_validation_message_is_returned(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i in range(diagnostics.MAX_ZIP_ENTRIES + 1):
                z.writestr(f"f{i}.txt", "x")
        r = self._upload(buf.getvalue())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "Too many files in diagnostics archive")

    def test_unexpected_error_is_hidden_in_response_and_index(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("manifest.json", "{}")
        with patch.object(diagnostics, "_build_index", side_effect=OSError(SECRET)), \
             self.assertLogs("sayit.errors", level="ERROR") as logs:
            r = self._upload(buf.getvalue())
        self.assertEqual(r.status_code, 500)
        body = r.json()
        self.assertNotIn("/srv/", json.dumps(body))
        ref = REF_RE.search(body["error"]).group(1)
        index = self._index(body["ticket_id"])
        self.assertNotIn("/srv/", json.dumps(index), "状态查询接口不能看到原文")
        self.assertEqual(index["error_code"], "internal")
        self.assertEqual(index["ref"], ref)
        self.assertIn(SECRET, "\n".join(logs.output))


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
