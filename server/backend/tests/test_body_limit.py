"""请求体上限（bodylimit.py）与中间件顺序。

验收看的是**有没有业务副作用**（写库、落盘），不是"处理函数有没有执行"：反馈接口的函数里
自己调 request.json()，计数是在它读的过程中触发的，函数其实已经进去了。
"""
from __future__ import annotations

import asyncio
import gc
import json
import tempfile
import unittest
import weakref
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.testclient import TestClient

from backend.app import diagnostics, main
from backend.app.bodylimit import BodyLimitMiddleware, BodyLimitRule, build_body_limit_rules
from backend.app.config import BodyLimitConfig, _load_body_limits

ORIGIN = "http://tauri.localhost"


def _run_asgi(app, *, path: str, chunks: list[bytes], headers: list[tuple[bytes, bytes]] | None = None):
    """直接按 ASGI 协议跑一次请求，逐块喂请求体。返回 (状态码, 响应体, 内层收到的消息)。"""
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers or [],
        "client": ("127.0.0.1", 1),
    }
    pending = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    sent: list[dict] = []

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return (start["status"] if start else None), body, sent


class _Recorder:
    """内层应用：把请求体读到底，记下是否读完、读到多少。"""

    def __init__(self):
        self.completed = False
        self.got = 0
        self.saw_disconnect = False

    async def __call__(self, scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.saw_disconnect = True
                await send({"type": "http.response.start", "status": 400, "headers": []})
                await send({"type": "http.response.body", "body": b"inner-400"})
                return
            self.got += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        self.completed = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _mw(inner, limit=100):
    return BodyLimitMiddleware(
        inner,
        exact={"/x": BodyLimitRule(limit, {"error": "too_big"})},
        prefixes=[("/admin/", BodyLimitRule(limit * 2, {"error": "admin_too_big"}))],
        default=BodyLimitRule(10, {"error": "default_too_big"}),
    )


class MiddlewareUnitTests(unittest.TestCase):
    def test_declared_length_over_limit_rejects_without_reading(self):
        inner = _Recorder()
        status, body, _ = _run_asgi(_mw(inner), path="/x", chunks=[b"a" * 50], headers=[(b"content-length", b"1000")])
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body), {"error": "too_big"})
        self.assertEqual(inner.got, 0, "声明值已超限时内层一个字节都不该读到")

    def test_chunked_without_length_is_counted(self):
        inner = _Recorder()
        status, body, sent = _run_asgi(_mw(inner), path="/x", chunks=[b"a" * 60, b"a" * 60, b"a" * 60])
        self.assertEqual(status, 413)
        self.assertFalse(inner.completed)
        self.assertTrue(inner.saw_disconnect, "超限后内层应看到断开，解析就此中止")
        self.assertNotIn(b"inner-400", body, "内层随后的 400 必须被丢掉")
        self.assertEqual(sum(1 for m in sent if m["type"] == "http.response.start"), 1)

    def test_understated_content_length_is_still_caught(self):
        inner = _Recorder()
        status, _, _ = _run_asgi(
            _mw(inner), path="/x", chunks=[b"a" * 80, b"a" * 80], headers=[(b"content-length", b"10")]
        )
        self.assertEqual(status, 413)
        self.assertFalse(inner.completed)

    def test_within_limit_passes_through(self):
        inner = _Recorder()
        status, body, _ = _run_asgi(_mw(inner), path="/x", chunks=[b"a" * 50, b"a" * 50])
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")
        self.assertEqual(inner.got, 100)

    def test_rule_selection(self):
        mw = _mw(_Recorder())
        self.assertEqual(mw.rule_for("/x").payload, {"error": "too_big"})
        self.assertEqual(mw.rule_for("/admin/api/feedback/1/image").payload, {"error": "admin_too_big"})
        self.assertEqual(mw.rule_for("/admin").payload, {"error": "admin_too_big"})
        self.assertEqual(mw.rule_for("/x/withdraw").payload, {"error": "default_too_big"})
        self.assertEqual(mw.rule_for("/administrator").payload, {"error": "default_too_big"})

    def test_websocket_scope_is_untouched(self):
        called = []

        async def inner(scope, receive, send):
            called.append(scope["type"])

        asyncio.run(_mw(inner)({"type": "websocket", "path": "/ws/transcribe", "headers": []}, None, None))
        self.assertEqual(called, ["websocket"])


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        c = BodyLimitConfig()
        self.assertEqual(c.diagnostics, 52 * 1024 * 1024)
        self.assertEqual(c.asr_corrections, 11 * 1024 * 1024)
        self.assertEqual(c.feedback, 512 * 1024)
        self.assertEqual(c.admin, 8 * 1024 * 1024)
        self.assertEqual(c.default, 1024 * 1024)

    def test_yaml_overrides_and_bad_values(self):
        c = _load_body_limits({"feedback_kib": 1024, "diagnostics_mib": "x", "admin_mib": True, "nope": 1})
        self.assertEqual(c.feedback, 1024 * 1024)
        self.assertEqual(c.diagnostics, BodyLimitConfig().diagnostics)
        self.assertEqual(c.admin, BodyLimitConfig().admin)

    def test_rules_keep_client_error_formats(self):
        rules = build_body_limit_rules(BodyLimitConfig())
        # 客户端 asrCorrection.ts 按 error == "audio_too_large" 显示"录音太大"
        self.assertEqual(rules["exact"]["/api/asr-corrections"].payload["error"], "audio_too_large")


class MainAppTests(unittest.TestCase):
    """接在真实的 main.app 上：顺序、CORS 头、副作用。"""

    def setUp(self):
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))

    def test_feedback_over_limit_has_cors_and_no_db_write(self):
        db = MagicMock()
        big = {"machine_id": "m1", "feedback_text": "hello", "context": {"x": "a" * (600 * 1024)}}
        with patch.object(main, "database", db):
            r = self.client.post("/api/feedback", json=big, headers={"Origin": ORIGIN})
        self.assertEqual(r.status_code, 413)
        self.assertIn("access-control-allow-origin", r.headers, "413 没有 CORS 头，桌面端只会看到网络错误")
        db.execute.assert_not_called()

    def test_feedback_chunked_over_limit_no_db_write(self):
        db = MagicMock()

        def gen():
            yield b'{"machine_id":"m1","feedback_text":"hello","context":{"x":"'
            for _ in range(80):
                yield b"a" * 8192
            yield b'"}}'

        with patch.object(main, "database", db):
            r = self.client.post("/api/feedback", content=gen(), headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)
        db.execute.assert_not_called()

    def test_feedback_normal_size_still_works(self):
        db = MagicMock()
        db.dialect.placeholder = "?"
        ok = {"machine_id": "m-normal-size", "feedback_text": "正常反馈" * 100, "context": {"ai_system_prompt": "提" * 5000}}
        with patch.object(main, "database", db):
            r = self.client.post("/api/feedback", json=ok, headers={"Origin": ORIGIN})
        self.assertEqual(r.status_code, 200, r.text)
        db.execute.assert_called_once()

    def test_diagnostics_over_limit_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(diagnostics, "DIAGNOSTICS_DIR", Path(tmp)):
            big = b"\0" * (53 * 1024 * 1024)
            r = self.client.post(
                "/api/diagnostics",
                files={"diagnostics": ("diagnostics.zip", big, "application/zip")},
                headers={"Origin": ORIGIN},
            )
            self.assertEqual(r.status_code, 413)
            self.assertIn("access-control-allow-origin", r.headers)
            self.assertEqual(list(Path(tmp).iterdir()), [], "超限的诊断包不能留下任何目录或文件")

    def test_asr_correction_over_limit_uses_client_error_code(self):
        r = self.client.post(
            "/api/asr-corrections",
            data={"correction_id": "abcdefgh", "machine_id": "m"},
            files={"audio": ("a.wav", b"\0" * (12 * 1024 * 1024), "audio/wav")},
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["error"], "audio_too_large")

    def test_rate_limit_429_has_cors_headers(self):
        # 用独立的对端地址，免得把别的测试共用的限流桶耗光
        client = TestClient(main.app, client=("198.51.100.77", 50000))
        statuses = [client.get("/api/notice", headers={"Origin": ORIGIN}) for _ in range(40)]
        limited = [r for r in statuses if r.status_code == 429]
        self.assertTrue(limited, "40 次连续请求应触发限流")
        self.assertIn("access-control-allow-origin", limited[0].headers, "429 没有 CORS 头（2b）")


class MultipartTempFileTests(unittest.TestCase):
    """multipart 中途超限：框架建的临时文件必须被关掉，最终回 413 而不是 400/500。"""

    def test_spooled_files_are_closed(self):
        import starlette.formparsers as fp

        created = []
        real = fp.SpooledTemporaryFile

        def tracking(*args, **kwargs):
            f = real(*args, **kwargs)
            # 只存弱引用：存强引用的话测试自己就把文件留住了，判断不了框架有没有放掉它
            created.append(weakref.ref(f))
            return f

        app = FastAPI()

        @app.post("/up")
        async def up(file: UploadFile = File(...)):  # pragma: no cover - 超限时不该走到这里
            return {"size": len(await file.read())}

        wrapped = BodyLimitMiddleware(
            app, exact={"/up": BodyLimitRule(3 * 1024 * 1024, {"error": "too_big"})}, prefixes=[],
            default=BodyLimitRule(1024, {"error": "default"}),
        )
        boundary = "XyZboundary"

        def gen():
            # 分块发送、不带 Content-Length：这样中间件只能边收边数，解析器已经在往临时文件里写
            yield (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.bin\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            for _ in range(5 * 16):
                yield b"\0" * (64 * 1024)
            yield f"\r\n--{boundary}--\r\n".encode()

        # TestClient 会把请求体攒成一整块再交给应用，测不出"解析到一半"，所以直接按 ASGI 逐块喂
        with patch.object(fp, "SpooledTemporaryFile", tracking):
            status, body, _ = _run_asgi(
                wrapped,
                path="/up",
                chunks=list(gen()),
                headers=[(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
            )
        self.assertEqual(status, 413, body)
        self.assertTrue(created, "测试前提：解析器应当建过临时文件")
        # Starlette 只在 MultiPartException 时主动关文件；中间件在拒绝路径上做一次 gc.collect()
        # 把挂在引用环上的文件对象释放掉。这里**不再**自己调 gc：要验的就是中间件做了这件事
        alive_open = [r() for r in created if r() is not None and not r().closed]
        self.assertEqual(alive_open, [], "超限后临时文件没有被释放")


if __name__ == "__main__":
    unittest.main()
