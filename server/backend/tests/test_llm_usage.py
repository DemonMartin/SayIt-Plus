"""润色调用报回的用量：guard 按它给润色额度记账。不发真实请求，HTTP 客户端换成假的。"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from backend.app.config import LLMProfile
from backend.app.llm import LLMEngine


def _engine() -> LLMEngine:
    return LLMEngine(LLMProfile(provider="openai", enabled=True, prompt_dir="/nonexistent"))


def _response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = data
    return resp


def _polish(engine: LLMEngine, post: AsyncMock) -> tuple[str, int, dict]:
    engine._client = MagicMock(post=post)
    return asyncio.run(engine.polish("原始识别文本", system_prompt="客户端提示词"))


class LlmUsageTests(unittest.TestCase):
    def test_reported_usage_is_passed_through(self) -> None:
        data = {
            "choices": [{"message": {"content": "润色后的文本"}}],
            "usage": {"prompt_tokens": 321, "completion_tokens": 45, "total_tokens": 366},
        }
        text, _, debug = _polish(_engine(), AsyncMock(return_value=_response(data)))
        self.assertEqual(text, "润色后的文本")
        self.assertEqual(debug["usage"], {"input_tokens": 321, "output_tokens": 45, "source": "api"})

    def test_missing_usage_is_estimated_from_what_was_sent(self) -> None:
        engine = _engine()
        data = {"choices": [{"message": {"content": "润色后的文本"}}]}
        _, _, debug = _polish(engine, AsyncMock(return_value=_response(data)))
        sent = engine._build_messages("原始识别文本", "客户端提示词")
        self.assertEqual(debug["usage"], {
            "input_tokens": sum(len(m["content"]) for m in sent),
            "output_tokens": len("润色后的文本"),
            "source": "estimate",
        })

    def test_failed_call_still_reports_estimated_input(self) -> None:
        engine = _engine()
        text, ms, debug = _polish(engine, AsyncMock(side_effect=RuntimeError("upstream 500")))
        self.assertEqual((text, ms), ("原始识别文本", 0))  # 失败时照旧返回原文
        self.assertIn("error", debug)
        self.assertEqual(debug["usage"]["source"], "estimate")
        self.assertGreater(debug["usage"]["input_tokens"], 0)
        self.assertEqual(debug["usage"]["output_tokens"], 0)

    def test_garbage_usage_falls_back_to_estimate(self) -> None:
        for usage in (None, "x", {}, {"prompt_tokens": "lots"}, {"prompt_tokens": 0, "completion_tokens": 0}):
            with self.subTest(usage=usage):
                self.assertIsNone(LLMEngine._extract_usage({"usage": usage}))


if __name__ == "__main__":
    unittest.main()
