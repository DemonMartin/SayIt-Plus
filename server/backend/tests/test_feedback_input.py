"""反馈接口输入校验（feedback_input.py）+ 计数字典（KeyedWindowLimiter）。"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.feedback_input import FeedbackInputError, parse_feedback
from backend.app.ratelimit import KeyedWindowLimiter


def client_payload() -> dict:
    """照 client/src/services/feedback.ts 当前版本的真实格式造一份。"""
    return {
        "machine_id": "sayit-11111111-2222-3333-4444-555555555555",
        "app_version": "0.2.2",
        "timestamp": "2026-10-10T08:00:00.000Z",
        "feedback_text": "识别有时候会把热词识别错",
        "transcript": {
            "asr_text": "有三个事情先修复漏洞的 bug 吧",
            "ai_text": "1. 修复漏洞的 Bug。",
            "duration_sec": 5.6,
            "app_name": "Visual Studio Code",
            "app_id": "code",
            "window_title": "main.py - SayIt - Visual Studio Code",
            "process_name": "Code.exe",
            "window_class": "Chrome_WidgetWin_1",
            "asr_ms": 1199,
            "llm_ms": 566,
            "audio_duration_sec": 5.6,
            "char_count": 12,
            "is_empty": False,
            "asr_provider": "doubao_v2",
            "ai_provider": "deepseek",
            "ai_model": "deepseek-v4-flash",
            "prompt_summary": "Base preset id: intent | App rule: no match",
            "auto_applied_hotwords": ["SayIt", "GitHub"],
            "record_timestamp": 1791561026000,
        },
        "context": {
            "work_mode": "cloud_api",
            "asr_provider": "doubao_v2",
            "asr_model": "",
            "ai_enabled": True,
            "ai_provider": "deepseek",
            "ai_model": "deepseek-v4-flash",
            "ai_preset_name": "意图整理",
            "ai_system_prompt": "你是语音文本精炼助手。",
            "ai_prompt_append": "",
            "platform": "windows",
            "os_version": "Microsoft Windows [版本 10.0.26100.4652]",
            "system_locale": "zh-CN",
            "cpu_cores": 16,
            "memory_mb": 32480,
        },
    }


def old_behaviour(body: dict) -> tuple:
    """改动前 main.post_feedback 的入库逻辑（只截断四个长字段，其余原样）。"""
    transcript = body.get("transcript")
    if isinstance(transcript, dict):
        for k in ("asr_text", "ai_text"):
            if isinstance(transcript.get(k), str) and len(transcript[k]) > 5000:
                transcript[k] = transcript[k][:5000]
    else:
        transcript = None
    context = body.get("context")
    if isinstance(context, dict):
        for k in ("ai_system_prompt", "ai_prompt_append"):
            if isinstance(context.get(k), str) and len(context[k]) > 5000:
                context[k] = context[k][:5000]
    else:
        context = None
    return (
        body["machine_id"].strip(),
        (body.get("app_version") or "")[:64],
        body["feedback_text"].strip(),
        json.dumps(transcript, ensure_ascii=False) if transcript else None,
        json.dumps(context, ensure_ascii=False) if context else None,
    )


def new_behaviour(body: dict) -> tuple:
    p = parse_feedback(body)
    return (
        p["machine_id"],
        p["app_version"],
        p["feedback_text"],
        json.dumps(p["transcript"], ensure_ascii=False) if p["transcript"] else None,
        json.dumps(p["context"], ensure_ascii=False) if p["context"] else None,
    )


class ParseFeedbackTests(unittest.TestCase):
    def test_current_client_payload_is_stored_exactly_as_before(self):
        self.assertEqual(new_behaviour(client_payload()), old_behaviour(client_payload()))

    def test_without_transcript_matches_before(self):
        body = client_payload()
        body["transcript"] = None
        self.assertEqual(new_behaviour(body), old_behaviour(dict(body)))

    def test_top_level_type_errors_are_400_not_500(self):
        for bad in ([], "x", 1, None):
            with self.assertRaises(FeedbackInputError):
                parse_feedback(bad)
        for machine_id in (123, "", "   ", "x" * 129, None, ["a"]):
            body = client_payload()
            body["machine_id"] = machine_id
            with self.assertRaises(FeedbackInputError, msg=repr(machine_id)):
                parse_feedback(body)
        for text in (5, None, "a", "x" * 1001):
            body = client_payload()
            body["feedback_text"] = text
            with self.assertRaises(FeedbackInputError, msg=repr(text)):
                parse_feedback(body)

    def test_unknown_fields_are_dropped(self):
        body = client_payload()
        body["context"]["evil"] = "x" * 1000
        body["transcript"]["__proto__"] = {"a": 1}
        p = parse_feedback(body)
        self.assertNotIn("evil", p["context"])
        self.assertNotIn("__proto__", p["transcript"])

    def test_number_and_bool_fields_are_strict(self):
        body = client_payload()
        body["context"]["cpu_cores"] = True          # bool 不算数字
        body["context"]["memory_mb"] = float("nan")  # NaN 不算
        body["transcript"]["asr_ms"] = "1199"        # 字符串不算
        body["transcript"]["llm_ms"] = float("inf")
        body["context"]["ai_enabled"] = 1            # 1 不算布尔
        body["transcript"]["is_empty"] = "false"
        p = parse_feedback(body)
        for key in ("cpu_cores", "memory_mb", "ai_enabled"):
            self.assertNotIn(key, p["context"], key)
        for key in ("asr_ms", "llm_ms", "is_empty"):
            self.assertNotIn(key, p["transcript"], key)
        self.assertEqual(p["transcript"]["char_count"], 12)

    def test_lengths_are_capped(self):
        body = client_payload()
        body["context"]["ai_system_prompt"] = "提" * 6000
        body["transcript"]["window_title"] = "w" * 900
        body["transcript"]["auto_applied_hotwords"] = ["h" * 100] * 300 + [5, None]
        body["app_version"] = "v" * 100
        p = parse_feedback(body)
        self.assertEqual(len(p["context"]["ai_system_prompt"]), 5000)
        self.assertEqual(len(p["transcript"]["window_title"]), 500)
        self.assertEqual(len(p["transcript"]["auto_applied_hotwords"]), 200)
        self.assertTrue(all(len(w) == 64 for w in p["transcript"]["auto_applied_hotwords"]))
        self.assertEqual(len(p["app_version"]), 64)

    def test_non_object_sections_become_none(self):
        body = client_payload()
        body["transcript"] = ["x"]
        body["context"] = "x"
        p = parse_feedback(body)
        self.assertIsNone(p["transcript"])
        self.assertIsNone(p["context"])


class LimiterTests(unittest.TestCase):
    def test_limit_per_key_within_window(self):
        lim = KeyedWindowLimiter(window_sec=600, limit=3)
        self.assertEqual([lim.allow("a", now=t) for t in (0, 1, 2, 3)], [True, True, True, False])
        self.assertTrue(lim.allow("b", now=3))
        self.assertTrue(lim.allow("a", now=601), "窗口过了应当恢复")

    def test_memory_is_bounded_when_keys_rotate(self):
        lim = KeyedWindowLimiter(window_sec=600, limit=3, max_keys=1000)
        for i in range(20_000):
            lim.allow(f"id-{i}", now=i * 0.01)
        self.assertLessEqual(len(lim), 1000)

    def test_cap_evicts_oldest_but_keeps_active_key_limited(self):
        lim = KeyedWindowLimiter(window_sec=600, limit=3, max_keys=10)
        for t in range(3):
            lim.allow("real-user", now=t)
        # 中间穿插一个刚活动过的 key，它应当留在最新端，不被挤掉
        for i in range(8):
            lim.allow(f"x{i}", now=3 + i * 0.1)
        self.assertFalse(lim.allow("real-user", now=4))

    def test_periodic_prune_removes_stale_keys(self):
        lim = KeyedWindowLimiter(window_sec=10, limit=3, prune_interval_sec=5)
        for i in range(100):
            lim.allow(f"k{i}", now=0)
        lim.allow("fresh", now=20)
        self.assertEqual(len(lim), 1)


class FeedbackEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app, client=("127.0.0.1", 50000))
        self.db = MagicMock()
        self.db.dialect.placeholder = "?"

    def _post(self, **kwargs):
        with patch.object(main, "database", self.db), \
             patch.object(main, "_feedback_limiter", KeyedWindowLimiter(window_sec=600, limit=3)):
            return self.client.post("/api/feedback", **kwargs)

    def test_bad_bodies_return_400(self):
        for kwargs in (
            {"json": []},
            {"json": {"machine_id": 5, "feedback_text": "hello"}},
            {"content": b"not json", "headers": {"Content-Type": "application/json"}},
            {"content": b"\xff\xfe\xfa", "headers": {"Content-Type": "application/json"}},
        ):
            r = self._post(**kwargs)
            self.assertEqual(r.status_code, 400, (kwargs, r.text))
        self.db.execute.assert_not_called()

    def test_valid_payload_is_stored(self):
        r = self._post(json=client_payload())
        self.assertEqual(r.status_code, 200, r.text)
        args = self.db.execute.call_args[0][1]
        self.assertEqual(args[0], client_payload()["machine_id"])
        self.assertEqual(json.loads(args[5]), client_payload()["context"])


if __name__ == "__main__":
    unittest.main()
