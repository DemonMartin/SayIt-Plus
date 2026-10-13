"""HTTP 请求体大小上限（纯 ASGI 中间件）。

为什么要在最外层拦，而不是在接口函数里数：FastAPI 在进入接口函数之前就会把表单和文件
整个收下、解析完（大文件先写临时目录），函数里"边读边数"那时数据早就收完了；
`await request.json()` 更是多大都收。所以上限必须在框架读请求体的那一层生效。

判据是**实际收到的字节数**，不信 `Content-Length`：声明值只用来提前拒绝（声明已超限就一个字节
都不读），声明写小、实际多发的请求靠累计计数拦住；分块上传没有这个头，也一样计数。

超限的处理：当场回 413，然后对内层应用假装"客户端已断开"。这样做而不是抛异常，是因为
FastAPI 解析表单时会把任何异常都转成 400（routing.py 里的 except Exception），抛出去的异常
到不了这里，用户就会看到一个含义错误的 400。内层应用之后再发的任何响应都会被丢掉。

这层要排在 CORS 中间件**里面**：413 也得带 CORS 头，否则桌面端的 fetch 会被浏览器拦下，
用户只看到"网络错误"。
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("sayit.bodylimit")

# 回 413 之后，最多再读掉多少请求体（规则上限的倍数）和多久，见 _reject 的说明
DRAIN_FACTOR = 2
DRAIN_SECONDS = 30


@dataclass(frozen=True, slots=True)
class BodyLimitRule:
    limit: int
    # 超限时回给客户端的 JSON。沿用各接口原有的错误格式，客户端已有的提示才能对上
    payload: dict


def _client_hint(scope) -> str:
    client = scope.get("client") or ("-", 0)
    for name, value in scope.get("headers") or []:
        if name == b"x-forwarded-for":
            return f"{client[0]} xff={value.decode('latin-1')[:120]}"
    return str(client[0])


def _declared_length(scope) -> int | None:
    for name, value in scope.get("headers") or []:
        if name == b"content-length":
            text = value.decode("latin-1").strip()
            return int(text) if text.isdigit() else None
    return None


class BodyLimitMiddleware:
    def __init__(
        self,
        app,
        *,
        exact: dict[str, BodyLimitRule],
        prefixes: list[tuple[str, BodyLimitRule]],
        default: BodyLimitRule,
    ) -> None:
        self.app = app
        self._exact = exact
        self._prefixes = prefixes
        self._default = default

    def rule_for(self, path: str) -> BodyLimitRule:
        rule = self._exact.get(path)
        if rule is not None:
            return rule
        for prefix, prefix_rule in self._prefixes:
            if path == prefix.rstrip("/") or path.startswith(prefix):
                return prefix_rule
        return self._default

    async def __call__(self, scope, receive, send):
        # WebSocket（口述）走另一条路，有自己的音频上限，这里不碰
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        rule = self.rule_for(path)
        declared = _declared_length(scope)

        if declared is not None and declared > rule.limit:
            drained = await self._reject(receive, send, rule, received=0, more_body=declared <= rule.limit * DRAIN_FACTOR)
            self._log(scope, path, rule, declared=declared, received=0, drained=drained)
            return

        received = 0
        rejected = False
        response_started = False

        async def guarded_receive():
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > rule.limit:
                    rejected = True
                    drained = 0
                    if not response_started:
                        drained = await self._reject(
                            receive, send, rule, received=received, more_body=message.get("more_body", False)
                        )
                    self._log(scope, path, rule, declared=declared, received=received, drained=drained)
                    # 对内层应用假装客户端断开：Starlette 读到它会抛 ClientDisconnect，
                    # 解析就此中止，后面写库、落盘的代码都走不到
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal response_started
            if rejected:
                # 413 已经发出去了，内层应用随后的 400/500 一律丢弃
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except Exception:
            # 只吞我们自己触发的那次中止（ClientDisconnect 或解析失败），其它异常照常抛出
            if not rejected:
                raise
        finally:
            if rejected:
                # multipart 解析到一半被中止时，Starlette 不会主动关掉已经建好的临时文件
                # （它只在 MultiPartException 时关），文件对象挂在异常与栈帧的引用环上，
                # 要等循环垃圾回收才释放。Linux 上那是已经 unlink 的匿名文件，/tmp 里看不见，
                # 却一直占着磁盘 —— 连续发超限请求就能攒起来。本地实测请求结束后仍留着一个。
                # 只在拒绝路径上做一次，量受限流约束，代价可以接受。
                gc.collect()

    @staticmethod
    async def _reject(receive, send, rule: BodyLimitRule, *, received: int, more_body: bool) -> int:
        """回 413。返回为此丢弃读掉的字节数。

        先发响应头，再把请求体剩下的部分读掉丢弃，最后才发响应体收尾。不读完就收尾的话，
        服务器会在客户端还在上传时关闭连接，内核里没读的数据会触发 TCP RST，客户端
        往往连 413 都收不到，只看到"连接被重置"——本地实测 52 MiB 诊断包就是这样。
        读掉的数据不进内存也不落盘，只花带宽；上限是规则上限的 DRAIN_FACTOR 倍和 DRAIN_SECONDS 秒，
        超过就直接收尾（那种体量只可能是恶意请求，让它吃 RST）。
        """
        body = json.dumps(rule.payload, ensure_ascii=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    # 这条连接不再复用
                    (b"connection", b"close"),
                ],
            }
        )
        drained = 0
        cap = rule.limit * DRAIN_FACTOR
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DRAIN_SECONDS
        while more_body and received + drained <= cap:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if message["type"] != "http.request":
                break
            drained += len(message.get("body", b""))
            more_body = message.get("more_body", False)
        await send({"type": "http.response.body", "body": body, "more_body": False})
        return drained

    @staticmethod
    def _log(scope, path: str, rule: BodyLimitRule, *, declared: int | None, received: int, drained: int) -> None:
        logger.warning(
            "body limit exceeded path=%s method=%s limit=%d declared=%s received=%d drained=%d client=%s",
            path,
            scope.get("method", "-"),
            rule.limit,
            "-" if declared is None else declared,
            received,
            drained,
            _client_hint(scope),
        )


def build_body_limit_rules(limits) -> dict:
    """由 config 的 BodyLimitConfig 生成中间件参数。错误格式与各接口原有的保持一致。"""
    return {
        "exact": {
            "/api/diagnostics": BodyLimitRule(
                limits.diagnostics, {"error": "Diagnostics package is too large"}
            ),
            # 只有提交本身放宽；撤回接口（/api/asr-corrections/{id}/withdraw）走默认上限
            "/api/asr-corrections": BodyLimitRule(
                limits.asr_corrections,
                {"error": "audio_too_large", "message": "Request body is too large"},
            ),
            "/api/feedback": BodyLimitRule(limits.feedback, {"error": "payload too large"}),
        },
        # admin 后台能上传 5 MiB 截图（base64 后约 6.7 MiB）
        "prefixes": [("/admin/", BodyLimitRule(limits.admin, {"error": "payload too large"}))],
        "default": BodyLimitRule(limits.default, {"error": "payload too large"}),
    }
