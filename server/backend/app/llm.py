"""LLM engine: text polishing via Azure OpenAI / OpenAI-compatible / Groq / Ollama."""
from __future__ import annotations

import json
import html
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .config import LLMProfile

logger = logging.getLogger("sayit.llm")


def _read_prompt(path: str, fallback: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return fallback


_DEFAULT_SYSTEM_PROMPT = """\
你是语音文本精炼助手。输入是 ASR 语音识别的原始转写，你的任务是清洗为可直接使用的干净文本。
核心原则：保留用户全部有效信息，只清除语音噪声和识别错误。

处理规则：
1. 移除口语填充词（嗯、啊、那个、就是说、然后呢）和无意义的重复、犹豫。
2. 识别自我修正——"不对"、"不是"、"应该是"、"改到"后以最终表达为准，删除前序错误。
3. 修正明显的语音识别错误：同音字、音近字、专有名词、英文大小写、数字和时间。
4. 添加标点符号，必要时分段。中英文混合保留合理空格。
5. 检测到"第一/第二/首先/然后"等结构化表达时，输出为有序列表。

约束：
- 不添加原文没有的内容，不改变用户核心语义
- 不回答、解释、总结或续写文本中提到的问题

只输出精炼后的文本。"""

_DEFAULT_USER_PREFIX = "请校对以下 <asr_text> 标签内的语音转写文本：\n\n<asr_text>"

# 桌面客户端模式下用户消息的外壳：不含任何动词/祈使句，只用一对标签给待清洗文本
# 标出边界（模型输出后会被 _strip_thinking 清掉，不会混进结果）。这条壳文字固定、
# 不可配置——桌面模式的全部 prompt 措辞（风格、约束）都由客户端的 system_prompt
# 决定，服务器不再额外"发出"任何指令，避免壳文字与文本内容本身恰好都是祈使句时
# （如 ASR 说的是"请执行"）被模型误当成连续指令。
_DESKTOP_USER_WRAPPER = "<asr_text>\n{text}\n</asr_text>"


def _desktop_user_content(text: str, context: dict[str, str] | None = None) -> str:
    asr = html.escape(text, quote=True)
    if not context:
        return _DESKTOP_USER_WRAPPER.format(text=asr)

    before = str(context.get("text_before") or "")[-500:]
    selected = str(context.get("selected_text") or "")[:6000]
    after = str(context.get("text_after") or "")[:300]
    if not (before or selected or after):
        return _DESKTOP_USER_WRAPPER.format(text=asr)
    source = str(context.get("source") or "")[:64]
    return (
        f'<text_context source="{html.escape(source, quote=True)}">\n'
        f'<text_before>{html.escape(before, quote=True)}</text_before>\n'
        f'<selected_text>{html.escape(selected, quote=True)}</selected_text>\n'
        f'<text_after>{html.escape(after, quote=True)}</text_after>\n'
        f'</text_context>\n{_DESKTOP_USER_WRAPPER.format(text=asr)}'
    )


class LLMEngine:
    def __init__(self, profile: LLMProfile) -> None:
        self._cfg = profile
        provider = profile.provider.lower()
        timeout_map = {"azure": profile.azure_timeout, "openai": profile.openai_timeout, "groq": profile.groq_timeout, "ollama": profile.ollama_timeout}
        timeout = timeout_map.get(provider, 15)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=8.0))
        if provider == "openai":
            # 启动摘要只写 provider / model，看不出 temperature 到底发没发。改成可省略之后，
            # "配置生效了"和"还在跑旧默认值"在外部行为上没有区别，所以把实际会发出去的
            # 载荷开关直接落一行日志，作为部署后的验证判据。
            logger.info(
                "LLM openai payload config: base_url=%s model=%s temperature=%s reasoning_effort=%s",
                profile.openai_base_url, profile.openai_model,
                "omitted" if profile.openai_temperature is None else profile.openai_temperature,
                profile.openai_reasoning_effort or "omitted",
            )

    async def close(self):
        await self._client.aclose()

    # ── public API ──

    async def polish(
        self, text: str, system_prompt: str | None = None,
        text_context: dict[str, str] | None = None, *, is_web_demo: bool = False,
    ) -> tuple[str, int, dict]:
        """Polish text, return (result, elapsed_ms, debug_info).

        is_web_demo=False（桌面客户端，默认）：system prompt 完全由调用方（客户端）
        提供，用户消息用固定的中性标签包一下，服务器不额外拼任何指令措辞。
        is_web_demo=True：网页体验版没有客户端，永远使用服务器本地 prompts/ 目录下的
        system.txt / user.txt（或内置默认值），忽略传入的 system_prompt（即使有）。
        """
        if not text or not self._cfg.enabled:
            return text, 0, {"skipped": True}

        messages = self._build_messages(
            text, system_prompt, text_context=text_context, is_web_demo=is_web_demo,
        )
        provider = self._cfg.provider.lower()

        try:
            t = time.monotonic()
            if provider == "azure":
                raw, usage = await self._call_azure(messages)
            elif provider == "openai":
                raw, usage = await self._call_openai(messages)
            elif provider == "groq":
                raw, usage = await self._call_groq(messages)
            else:
                raw, usage = await self._call_ollama(messages)
            ms = int((time.monotonic() - t) * 1000)

            result = self._strip_thinking(raw) or text
            if self._cfg.debug_llm:
                logger.info("LLM %s %dms: %s", provider, ms, result[:200])
            if provider == "openai":
                model = self._cfg.openai_model
            elif provider == "azure":
                model = self._cfg.azure_deployment
            elif provider == "groq":
                model = self._cfg.groq_model
            else:
                model = self._cfg.ollama_model
            debug = {
                "provider": provider,
                "model": model,
                # guard 按它给润色额度记账（只有数字，不含任何文本）。接口没报用量时按字数估。
                "usage": usage or self._estimate_usage(messages, raw),
            }
            if self._cfg.debug_llm and not text_context:
                debug["messages"] = messages
                debug["raw_output"] = raw
            elif text_context:
                debug["text_context"] = {
                    "source": str(text_context.get("source") or "")[:64],
                    "before_len": len(str(text_context.get("text_before") or "")),
                    "selected_len": len(str(text_context.get("selected_text") or "")),
                    "after_len": len(str(text_context.get("text_after") or "")),
                }
            return result, ms, debug
        except Exception as e:
            logger.exception("LLM %s failed, returning original", provider)
            # 失败的调用也按输入估一份用量：请求可能已经计费，而且反复失败也不该能白调
            return text, 0, {"error": str(e), "usage": self._estimate_usage(messages, "")}

    # ── internals ──

    def _build_messages(
        self, text: str, system_prompt: str | None = None,
        text_context: dict[str, str] | None = None, *, is_web_demo: bool = False,
    ) -> list[dict]:
        if not is_web_demo:
            # 桌面模式：system prompt 全由客户端决定（服务器本地 system.txt 仅在客户端
            # 异常未传时兜底）；用户消息固定用中性标签包裹，不含任何指令措辞。
            sys_p = system_prompt or _read_prompt(
                f"{self._cfg.prompt_dir}/system.txt", _DEFAULT_SYSTEM_PROMPT
            )
            user_content = _desktop_user_content(text, text_context)
            return [{"role": "system", "content": sys_p}, {"role": "user", "content": user_content}]

        # Web demo：没有客户端，永远用服务器本地 prompts/ 目录（或内置默认值）兜底——
        # 即使调用方误传了 system_prompt 也忽略，双重保险防止客户端预设泄露到公开体验页。
        d = self._cfg.prompt_dir
        sys_p = _read_prompt(f"{d}/system.txt", _DEFAULT_SYSTEM_PROMPT)
        user_prefix = _read_prompt(f"{d}/user.txt", _DEFAULT_USER_PREFIX)
        user_content = f"{user_prefix}{text}\n</asr_text>" if "<asr_text>" in user_prefix else f"{user_prefix}{text}"
        return [{"role": "system", "content": sys_p}, {"role": "user", "content": user_content}]

    @staticmethod
    def _strip_thinking(text: str) -> str:
        if not text:
            return ""
        c = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.DOTALL)
        c = re.sub(r"</?asr_text>", "", c, flags=re.I)
        if "最终答案" in c:
            c = c.split("最终答案", 1)[-1]
        c = re.sub(r"^\s*[:：]\s*", "", c)
        return c.strip()

    @staticmethod
    def _extract_text(data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = (choices[0] or {}).get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(
                it.get("text", "") for it in content
                if isinstance(it, dict) and it.get("type") == "text"
            ).strip()
        return ""

    @staticmethod
    def _extract_usage(data: Any) -> dict | None:
        """OpenAI 兼容接口报回的 token 用量；没有就返回 None，由调用方按字数估。

        completion_tokens 已经包含推理模型的思考 token（计费也按它算），不用另加。
        """
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return None
        try:
            inp = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            return None
        if inp <= 0 and out <= 0:
            return None
        return {"input_tokens": inp, "output_tokens": out, "source": "api"}

    @staticmethod
    def _estimate_usage(messages: list[dict], output: str) -> dict:
        """接口没报用量时按字数估：一个字符算一个 token。中文接近，英文偏多，宁可多算。"""
        def _len(content: Any) -> int:
            return len(content) if isinstance(content, str) else len(str(content or ""))

        return {
            "input_tokens": sum(_len(m.get("content")) for m in messages),
            "output_tokens": len(output or ""),
            "source": "estimate",
        }

    async def _call_azure(self, messages: list[dict]) -> tuple[str, dict | None]:
        ep = self._cfg.azure_endpoint.rstrip("/")
        dep = self._cfg.azure_deployment
        ver = self._cfg.azure_api_version
        url = f"{ep}/openai/deployments/{dep}/chat/completions?api-version={ver}"
        payload = {"messages": messages, "temperature": 0.2, "max_tokens": 2048}
        resp = await self._client.post(
            url, json=payload,
            headers={"api-key": self._cfg.azure_api_key},
            timeout=self._cfg.azure_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return self._extract_text(data), self._extract_usage(data)

    @staticmethod
    def _chat_url(base_url: str) -> str:
        base = base_url.rstrip("/")
        # Strip trailing /v1 if present, then always append /v1/chat/completions
        if base.endswith("/v1"):
            base = base[:-3]
        return f"{base}/v1/chat/completions"

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """非 2xx 时先把响应体记进日志，再抛。

        httpx 的 raise_for_status 只带状态码和 URL，而"哪个字段不被支持"只写在响应体里；
        polish() 又会把异常吞成"原文返回"。不记 body 的话，载荷不兼容（如推理模型拒收
        temperature）和鉴权失败在日志里长得一模一样，只剩一句 400。
        """
        if resp.is_success:
            return
        logger.warning(
            "LLM HTTP %s from %s: %s",
            resp.status_code, resp.request.url, resp.text.replace("\n", " ")[:300],
        )
        resp.raise_for_status()

    def _openai_payload(self, messages: list[dict]) -> dict:
        """OpenAI 兼容载荷。

        temperature 支持整体省略：推理模型只接受该参数的默认值，显式发 0.2 会被 400 拒收，
        所以适配手段是"不发这个字段"，而不是换一个数值。
        """
        payload: dict = {"model": self._cfg.openai_model, "messages": messages}
        if self._cfg.openai_temperature is not None:
            payload["temperature"] = self._cfg.openai_temperature
        if self._cfg.openai_reasoning_effort:
            payload["reasoning_effort"] = self._cfg.openai_reasoning_effort
        return payload

    async def _call_openai(self, messages: list[dict]) -> tuple[str, dict | None]:
        url = self._chat_url(self._cfg.openai_base_url)
        resp = await self._client.post(
            url, json=self._openai_payload(messages),
            headers={"Authorization": f"Bearer {self._cfg.openai_api_key}"},
            timeout=self._cfg.openai_timeout,
        )
        self._raise_for_status(resp)
        data = resp.json()
        return self._extract_text(data), self._extract_usage(data)

    async def _call_groq(self, messages: list[dict]) -> tuple[str, dict | None]:
        url = self._chat_url(self._cfg.groq_base_url)
        payload = {"model": self._cfg.groq_model, "temperature": 0.2, "messages": messages}
        resp = await self._client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {self._cfg.groq_api_key}"},
            timeout=self._cfg.groq_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return self._extract_text(data), self._extract_usage(data)

    async def _call_ollama(self, messages: list[dict]) -> tuple[str, dict | None]:
        combined = "\n\n".join(m["content"] for m in messages)
        payload = {"model": self._cfg.ollama_model, "stream": False, "prompt": combined}
        resp = await self._client.post(
            self._cfg.ollama_url, json=payload, timeout=self._cfg.ollama_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = None
        try:
            inp, out = int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
            if inp > 0 or out > 0:
                usage = {"input_tokens": inp, "output_tokens": out, "source": "api"}
        except (TypeError, ValueError, AttributeError):
            usage = None
        return (data.get("response") or "").strip(), usage
