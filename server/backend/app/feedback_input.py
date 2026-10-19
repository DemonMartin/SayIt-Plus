"""首页「意见反馈」接口的输入校验。

以前除了四个长字段截断之外，transcript / context 两个对象原样入库：结构和内容都由发送方决定，
而且顶层不查类型 —— 请求体是数组、或 machine_id 是数字时，.get() / .strip() 直接抛异常回 500。

字段清单取自 client/src/services/feedback.ts。客户端新增字段时这里要同步加，否则新字段会被
静默丢掉（只影响 admin 里能看到的信息，提交本身不会失败）。
"""
from __future__ import annotations

import math
from typing import Any, Callable

LONG_TEXT = 5000   # 识别原文、整理结果、两段提示词：与原有截断长度一致
SHORT_TEXT = 500   # 其余文字字段（窗口标题、模型名等），实际远小于这个数
MAX_HOTWORDS = 200       # 与纠错接口的热词规则一致
MAX_HOTWORD_CHARS = 64

MACHINE_ID_MAX = 128
APP_VERSION_MAX = 64
FEEDBACK_MIN, FEEDBACK_MAX = 2, 1000

_MISSING = object()


def _text(limit: int) -> Callable[[Any], Any]:
    def clean(value: Any) -> Any:
        return value[:limit] if isinstance(value, str) else _MISSING
    return clean


def _number(value: Any) -> Any:
    # Python 里 True/False 是 int 的子类，必须单独排除；json 解析默认还会接受 NaN / Infinity
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _MISSING
    if isinstance(value, float) and not math.isfinite(value):
        return _MISSING
    return value


def _boolean(value: Any) -> Any:
    return value if isinstance(value, bool) else _MISSING


def _hotwords(value: Any) -> Any:
    if not isinstance(value, list):
        return _MISSING
    words = [w[:MAX_HOTWORD_CHARS] for w in value if isinstance(w, str)]
    return words[:MAX_HOTWORDS]


CONTEXT_FIELDS: dict[str, Callable[[Any], Any]] = {
    "work_mode": _text(SHORT_TEXT),
    "asr_provider": _text(SHORT_TEXT),
    "asr_model": _text(SHORT_TEXT),
    "ai_enabled": _boolean,
    "ai_provider": _text(SHORT_TEXT),
    "ai_model": _text(SHORT_TEXT),
    "ai_preset_name": _text(SHORT_TEXT),
    "ai_system_prompt": _text(LONG_TEXT),
    "ai_prompt_append": _text(LONG_TEXT),
    "platform": _text(SHORT_TEXT),
    "os_version": _text(SHORT_TEXT),
    "system_locale": _text(SHORT_TEXT),
    "cpu_cores": _number,
    "memory_mb": _number,
}

TRANSCRIPT_FIELDS: dict[str, Callable[[Any], Any]] = {
    "asr_text": _text(LONG_TEXT),
    "ai_text": _text(LONG_TEXT),
    "duration_sec": _number,
    "app_name": _text(SHORT_TEXT),
    "app_id": _text(SHORT_TEXT),
    "window_title": _text(SHORT_TEXT),
    "process_name": _text(SHORT_TEXT),
    "window_class": _text(SHORT_TEXT),
    "asr_ms": _number,
    "llm_ms": _number,
    "audio_duration_sec": _number,
    "char_count": _number,
    "is_empty": _boolean,
    "asr_provider": _text(SHORT_TEXT),
    "ai_provider": _text(SHORT_TEXT),
    "ai_model": _text(SHORT_TEXT),
    "prompt_summary": _text(SHORT_TEXT),
    "auto_applied_hotwords": _hotwords,
    "record_timestamp": _number,
}


def clean_object(value: Any, fields: dict[str, Callable[[Any], Any]]) -> dict | None:
    """只留清单内、类型正确的字段。不是对象 → None（与原来"不是 dict 就当没有"一致）。"""
    if not isinstance(value, dict):
        return None
    out = {}
    for key, cleaner in fields.items():
        if key in value:
            cleaned = cleaner(value[key])
            if cleaned is not _MISSING:
                out[key] = cleaned
    return out


class FeedbackInputError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_feedback(body: Any) -> dict:
    """校验并清洗整个请求体。不合法抛 FeedbackInputError（调用方回 400）。"""
    if not isinstance(body, dict):
        raise FeedbackInputError("request body must be a JSON object")

    machine_id = body.get("machine_id")
    if not isinstance(machine_id, str) or not machine_id.strip() or len(machine_id.strip()) > MACHINE_ID_MAX:
        raise FeedbackInputError("machine_id required")

    feedback_text = body.get("feedback_text")
    feedback_text = feedback_text.strip() if isinstance(feedback_text, str) else ""
    if not FEEDBACK_MIN <= len(feedback_text) <= FEEDBACK_MAX:
        raise FeedbackInputError(f"feedback_text must be {FEEDBACK_MIN}-{FEEDBACK_MAX} chars")

    app_version = body.get("app_version")
    app_version = app_version[:APP_VERSION_MAX] if isinstance(app_version, str) else ""

    transcript = clean_object(body.get("transcript"), TRANSCRIPT_FIELDS)
    context = clean_object(body.get("context"), CONTEXT_FIELDS)
    return {
        "machine_id": machine_id.strip(),
        "feedback_text": feedback_text,
        "app_version": app_version,
        # 清洗后为空对象就按"没有"存，和原来一样不写 {}
        "transcript": transcript or None,
        "context": context or None,
    }
