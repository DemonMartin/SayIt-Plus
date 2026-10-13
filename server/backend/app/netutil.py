"""客户端真实 IP 解析。

取法与 nginx ``real_ip_recursive on`` 一致：只有直连对端在可信代理网段里，X-Forwarded-For
才有意义；然后从最右边往左跳过可信代理，第一个不可信的地址就是客户端。XFF 的最左值是
客户端自己写的，谁都能伪造，所以绝不能直接拿来当客户端 IP。

只认 X-Forwarded-For，不认 X-Real-IP：后者不是链，客户端也能自己带。

输入全部来自网络，这里的函数对任意输入都不抛异常。只用标准库。
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Sequence
from typing import Any, Union

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

_PORT_RE = re.compile(r"[0-9]{1,5}")
# nginx 把空格和逗号都当分隔符
_XFF_SPLIT_RE = re.compile(r"[\s,]+")
# 一个地址（含 [v6]:port、zone）不会超过这个长度；超长的直接当垃圾
_MAX_ENTRY_LEN = 64
# 真实代理链不会超过几跳。全是可信地址的超长 XFF 只可能是有人在拿它消耗 CPU，走到这里就停。
_MAX_TRUSTED_HOPS = 32


def _norm(raw: object) -> IPAddress | None:
    """把一个地址串解析成 IP，解析不了返回 None。

    允许 ``v4:port``、``[v6]``、``[v6]:port``、带 zone 的 v6。``::ffff:a.b.c.d``（IPv4 映射）
    归一成 IPv4：服务监听在 ``::`` 时对端地址就是这个形式，不归一的话 IPv4 的可信网段
    匹配不上它，同一个客户端也会因为监听方式不同变成两个 key。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or len(text) > _MAX_ENTRY_LEN:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None
        rest = text[end + 1:]
        if rest and not (rest[0] == ":" and _PORT_RE.fullmatch(rest[1:])):
            return None
        text = text[1:end]
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
        if not _PORT_RE.fullmatch(port):
            return None
        text = host
    text = text.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def parse_ip(raw: object) -> IPAddress | None:
    """公开的单地址解析（白名单匹配等用）。"""
    return _norm(raw)


def parse_networks(items: Iterable[Any] | None) -> tuple[list[IPNetwork], list[str]]:
    """把配置里的 CIDR / 单个 IP 解析成网段。写错的条目原样放进第二个返回值，由调用方报警告。"""
    nets: list[IPNetwork] = []
    invalid: list[str] = []
    for item in items or ():
        text = str(item).strip()
        try:
            nets.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            invalid.append(text)
    return nets, invalid


def ip_in(ip: IPAddress | str | None, nets: Sequence[IPNetwork]) -> bool:
    """ip 是否落在任一网段里。v4/v6 混比较时 ipaddress 返回 False，不会抛异常。"""
    addr = ip if isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else _norm(ip)
    if addr is None:
        return False
    return any(addr in net for net in nets)


def real_client_ip(peer: str | None, xff: str | None, trusted: Sequence[IPNetwork]) -> str:
    """按可信代理从右往左解析 X-Forwarded-For（nginx real_ip_recursive on）。

    - 直连对端不在可信网段 → 对端就是客户端，XFF 一概不看。
    - 对端可信 → XFF 从右往左：可信的跳过，第一个不可信的就是客户端。
    - 遇到解析不了的条目（"unknown"、垃圾）就停，取最后一个可信地址：再往左都是不可信的一方写的。
    - 全部可信 → 最左一个。
    - 对端本身解析不了（测试客户端的 "testclient" 等）→ 原样返回，当作不可信。
    """
    peer_ip = _norm(peer)
    if peer_ip is None:
        text = str(peer).strip() if peer is not None else ""
        return text or "unknown"
    current: IPAddress = peer_ip
    if not xff or not ip_in(current, trusted):
        return str(current)
    hops = 0
    for entry in reversed(_XFF_SPLIT_RE.split(str(xff))):
        if not entry:
            continue
        ip = _norm(entry)
        if ip is None:
            break
        current = ip
        if not ip_in(current, trusted):
            break
        hops += 1
        if hops >= _MAX_TRUSTED_HOPS:
            break
    return str(current)


def legacy_ip_key(direct: str | None, forwarded: str | None) -> str:
    """旧取法（guard.use_real_ip 未生效时）：XFF（或 X-Real-IP）最左一个，没有就用对端地址。

    这是 main._client_ip_key 原来的函数体，逐字搬过来的：use_real_ip 关着时行为必须与改动前完全一致。
    最左值可被伪造，只是在可信代理核对完成之前过渡用。
    """
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return (direct or "unknown").strip() or "unknown"


def xff_of(headers: Any) -> str | None:
    """把所有 X-Forwarded-For 头按出现顺序用 ", " 拼起来（nginx 合并同名头也是这样）。"""
    getlist = getattr(headers, "getlist", None)
    if getlist is None:
        return None
    joined = ", ".join(str(v) for v in getlist("x-forwarded-for") if v)
    return joined or None


class IpResolver:
    """可信代理网段 + 旧调用点要不要改用可信 IP。"""

    def __init__(self, trusted: Sequence[IPNetwork] = (), use_real_ip: bool = False) -> None:
        self.trusted: tuple[IPNetwork, ...] = tuple(trusted)
        self.use_real_ip = bool(use_real_ip)

    @property
    def configured(self) -> bool:
        return bool(self.trusted)

    @property
    def active(self) -> bool:
        # 没配可信网段时打开 use_real_ip，等于把每个人都解析成直连对端（网关/负载均衡的地址）：
        # 按 IP 的连接数上限和限流会把全站当成一个人。所以这种组合不生效，启动 banner 里报警告。
        return self.use_real_ip and self.configured

    def real_ip(self, peer: str | None, xff: str | None) -> str:
        return real_client_ip(peer, xff, self.trusted)

    def override(self, peer: str | None, headers: Any) -> str | None:
        """旧调用点的前缀分支：生效时返回可信 IP；否则 None，调用方照旧走原来的取法。"""
        if not self.active:
            return None
        return self.real_ip(peer, xff_of(headers))


def request_ip_override(request: Any) -> str | None:
    """HTTP 路由用：从 app.state.ip_resolver 取解析器。取不到（测试里的裸 app）就是 None = 旧取法。"""
    scope = getattr(request, "scope", None)
    app = scope.get("app") if isinstance(scope, dict) else None
    resolver = getattr(getattr(app, "state", None), "ip_resolver", None)
    if not isinstance(resolver, IpResolver):
        return None
    client = getattr(request, "client", None)
    return resolver.override(client.host if client else None, request.headers)
