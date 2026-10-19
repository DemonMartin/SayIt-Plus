"""Simple in-memory rate limiter middleware for FastAPI."""
from __future__ import annotations

import time
from collections import OrderedDict, defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .netutil import IpResolver

_PRUNE_INTERVAL = 300  # prune stale buckets every 5 minutes
_BUCKET_TTL = 600  # remove buckets idle for 10 minutes


class KeyedWindowLimiter:
    """按 key 计数的滑动窗口限流（反馈按 machine_id、诊断上传按 IP）。

    以前这两处各写了一个 `defaultdict(list)`，只在同一个 key 再来时清理它自己 —— key 由发送方
    决定（machine_id 自报、IP 取自可伪造的 X-Forwarded-For），不停换 key 字典就只增不减。
    这里加两道：定期整体清掉窗口外的记录；条数超过上限时先删最久没出现的 key。
    超上限时**删旧的而不是拒绝新的**：这个限流本来就能靠换 key 绕过，上限只为保护内存；
    拒绝新 key 反而可能把正常用户挡在外面。
    """

    def __init__(self, *, window_sec: float, limit: int, max_keys: int = 10_000, prune_interval_sec: float = 600) -> None:
        self._window = window_sec
        self._limit = limit
        self._max_keys = max_keys
        self._prune_interval = prune_interval_sec
        self._hits: OrderedDict[str, list[float]] = OrderedDict()
        self._last_prune: float | None = None

    def __len__(self) -> int:
        return len(self._hits)

    def allow(self, key: str, now: float | None = None) -> bool:
        """命中上限返回 False；否则记一次并返回 True。"""
        now = time.monotonic() if now is None else now
        self._prune(now)
        recent = [t for t in self._hits.get(key, ()) if now - t < self._window]
        if len(recent) >= self._limit:
            self._hits[key] = recent
            self._hits.move_to_end(key)
            return False
        recent.append(now)
        self._hits[key] = recent
        self._hits.move_to_end(key)
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)
        return True

    def _prune(self, now: float) -> None:
        if self._last_prune is None:
            self._last_prune = now
            return
        if now - self._last_prune < self._prune_interval:
            return
        self._last_prune = now
        stale = [k for k, ts in self._hits.items() if not ts or now - ts[-1] >= self._window]
        for k in stale:
            del self._hits[k]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limiter keyed by client IP.

    Parameters
    ----------
    app : ASGI app
    requests_per_minute : max requests per minute per IP (default 120)
    burst : max burst size (default 30)
    exclude_paths : path prefixes exempt from limiting (e.g. healthz)
    ip_resolver : 可信代理配置（guard.trusted_proxies / use_real_ip）。不传或未生效时沿用旧取法
    """

    def __init__(
        self,
        app,
        *,
        requests_per_minute: int = 120,
        burst: int = 30,
        exclude_paths: tuple[str, ...] = (),
        ip_resolver: IpResolver | None = None,
    ):
        super().__init__(app)
        self._rate = requests_per_minute / 60.0  # tokens per second
        self._burst = burst
        self._exclude = exclude_paths
        self._ip_resolver = ip_resolver
        self._buckets: dict[str, list[float]] = defaultdict(lambda: [float(burst), time.monotonic()])
        self._last_prune = time.monotonic()

    def _client_ip(self, request: Request) -> str:
        if self._ip_resolver is not None:
            real = self._ip_resolver.override(request.client.host if request.client else None, request.headers)
            if real is not None:
                return real
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _prune_stale(self) -> None:
        now = time.monotonic()
        if now - self._last_prune < _PRUNE_INTERVAL:
            return
        self._last_prune = now
        stale = [ip for ip, bucket in self._buckets.items() if now - bucket[1] > _BUCKET_TTL]
        for ip in stale:
            del self._buckets[ip]

    def _allow(self, ip: str) -> bool:
        bucket = self._buckets[ip]
        now = time.monotonic()
        elapsed = now - bucket[1]
        bucket[1] = now
        bucket[0] = min(self._burst, bucket[0] + elapsed * self._rate)
        if bucket[0] >= 1.0:
            bucket[0] -= 1.0
            return True
        return False

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in self._exclude):
            return await call_next(request)
        self._prune_stale()
        ip = self._client_ip(request)
        if not self._allow(ip):
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
        return await call_next(request)
