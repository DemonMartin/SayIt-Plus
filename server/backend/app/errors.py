"""发给客户端的内部错误提示。

异常原文里常有服务器内部路径、依赖名和版本、显卡报错、上游服务地址，对用户没用，对想攻击
服务器的人是现成的情报。所以客户端只拿到一句固定的话加一个编号，原文和堆栈只进服务端日志。

编号每次出错单独生成，不复用连接号 cid：一条连接上可能先后有好几次会话，只给 cid 对不到是哪一次。
日志行里 ref、cid、sid 三个都有，拿编号就能找到完整原文。
"""
from __future__ import annotations

import logging
import secrets

logger = logging.getLogger("sayit.errors")

try:  # httpx 的超时不是内置 TimeoutError 的子类
    import httpx

    _TIMEOUT_TYPES: tuple[type[BaseException], ...] = (TimeoutError, httpx.TimeoutException)
except ImportError:  # pragma: no cover
    _TIMEOUT_TYPES = (TimeoutError,)


def new_ref() -> str:
    return secrets.token_hex(4)


def is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, _TIMEOUT_TYPES)


def public_error_message(exc: BaseException, *, where: str, cid: str | None = None, sid: str | None = None) -> tuple[str, str]:
    """记一条带堆栈的日志，返回 (给客户端的话, 编号)。

    超时单独一句：客户端 errorMessages.ts 会识别 "timed out" 并显示"超时"那一类提示，
    改成通用提示的话用户就分不清是超时还是别的故障了。
    """
    ref = new_ref()
    logger.error("internal error ref=%s where=%s cid=%s sid=%s: %s", ref, where, cid or "-", sid or "-", exc, exc_info=exc)
    if is_timeout(exc):
        return f"Server processing timed out (ref: {ref})", ref
    return f"Internal server error (ref: {ref})", ref
