"""用量计数：滚动 24 小时窗口，按 5 分钟分桶，全部在内存里。

几条前提，改之前先想清楚：

- **假设只有一个 uvicorn worker。** 计数在进程内存里，开多个 worker 就是各数各的，
  所有上限实际变成 worker 数的倍数。要上多进程得先把计数挪到共享存储。
- 只在事件循环线程里调用，不加锁。调用方的「检查 → 登记」之间不能有 await，
  否则并发会话会一起通过检查。
- 额度只在 start 时按「已经用掉的」判，用量在会话处理完才记：同一设备同时开着的几条连接
  可以一起通过，超出的量以一个会话为限（每 IP 连接数上限兜底）。会话中途和 stop 时一律不拒。
- 进程重启（每次部署都会）后计数清零，启动时由 ``seed_audio_from_db`` 从 usage_sessions
  补回最近 24 小时的音频时长。润色用量补不回来：库里没有 token 数（也绝不能为此往库里存
  识别文本），所以重启后润色预算从 0 开始。
- 记账单位一律是整数：音频按毫秒、润色按 token（模型接口报回的输入 + 输出）。
"""
from __future__ import annotations

import heapq
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

WINDOW_MS = 24 * 60 * 60 * 1000
BUCKET_MS = 5 * 60 * 1000
# 每个窗口最多追踪的 key 数。超了按「最久没更新」淘汰（淘汰数进统计行），防止换着
# device_id 刷出无限多的 key 把内存吃满。
MAX_KEYS = 50_000

# 维度
DIM_DEVICE = "device"
DIM_IP = "ip"
DIM_NONLIVE_DEVICE = "nonlive_device"
DIM_NONLIVE_IP = "nonlive_ip"
DIM_DEMO_IP = "demo_ip"
DIM_INTERNAL = "internal"
AUDIO_DIMS = (DIM_DEVICE, DIM_IP, DIM_NONLIVE_DEVICE, DIM_NONLIVE_IP, DIM_DEMO_IP, DIM_INTERNAL)
AI_DIMS = (DIM_DEVICE, DIM_IP)
# 以「按代理链解析出来的客户端 IP」为 key 的维度。白名单（internal）不在里面：
# 白名单只认可信 IP，所以它的 key 本来就是可信的。
IP_DIMS = frozenset({DIM_IP, DIM_NONLIVE_IP, DIM_DEMO_IP})

# 分道
LANE_LIVE = "live"          # 桌面端实时录音（source == "live"）
LANE_NONLIVE = "nonlive"    # 桌面端其它来源：历史记录重新识别，以及任何自定义 source
LANE_DEMO = "web_demo"      # 网页体验（?client=web_demo）


def audio_keys(lane: str, *, device: str, ip: str, internal_ip: str | None = None) -> list[tuple[str, str]]:
    """一次会话的音频计到哪些 (维度, key) 上。检查、记账、seed 都走这一份，免得几处漂移。

    非 live 的会话**同时**计入设备 / IP 的总额和非 live 子额度：总额管「一个人一天最多用多少」，
    子额度管「不走实时录音的那部分最多多少」。只计子额度的话，把 source 改成别的值就能在
    总额之外再拿一份。
    """
    if internal_ip:
        return [(DIM_INTERNAL, internal_ip)]
    if lane == LANE_DEMO:
        return [(DIM_DEMO_IP, ip)]
    keys: list[tuple[str, str]] = []
    if device:
        keys.append((DIM_DEVICE, device))
    keys.append((DIM_IP, ip))
    if lane == LANE_NONLIVE:
        if device:
            keys.append((DIM_NONLIVE_DEVICE, device))
        keys.append((DIM_NONLIVE_IP, ip))
    return keys


def ai_keys(lane: str, *, device: str, ip: str) -> list[tuple[str, str]]:
    """润色用量计到哪些 (维度, key) 上：桌面端有 device_id 的计设备，所有分道都计 IP。"""
    keys: list[tuple[str, str]] = []
    if device and lane != LANE_DEMO:
        keys.append((DIM_DEVICE, device))
    keys.append((DIM_IP, ip))
    return keys


class RollingWindow:
    """按 key 的滚动窗口求和。

    桶号 = 时间戳 // bucket_ms；桶号 >= (now - window) // bucket_ms 的桶算在窗口里。
    所以一笔用量在窗口时长之后的 0~bucket_ms 内过期（宁可晚放，不会早放）。
    """

    def __init__(self, window_ms: int = WINDOW_MS, bucket_ms: int = BUCKET_MS, max_keys: int = MAX_KEYS) -> None:
        self.window_ms = int(window_ms)
        self.bucket_ms = int(bucket_ms)
        self.max_keys = max(1, int(max_keys))
        # key -> [合计, deque([桶号, 数量])]；OrderedDict 的顺序就是最近更新顺序
        self._data: OrderedDict[str, list[Any]] = OrderedDict()
        self.evicted = 0

    def _floor(self, now_ms: int) -> int:
        return (int(now_ms) - self.window_ms) // self.bucket_ms

    @staticmethod
    def _expire(entry: list[Any], floor: int) -> None:
        buckets: deque[list[int]] = entry[1]
        while buckets and buckets[0][0] < floor:
            entry[0] -= buckets.popleft()[1]

    def add(self, key: str, amount: int, now_ms: int) -> None:
        amount = int(amount)
        if amount <= 0 or not key:
            return
        now_ms = int(now_ms)
        bucket = now_ms // self.bucket_ms
        entry = self._data.get(key)
        if entry is None:
            entry = [0, deque()]
            self._data[key] = entry
        else:
            self._expire(entry, self._floor(now_ms))
            self._data.move_to_end(key)
        buckets = entry[1]
        # 时钟回拨时桶号可能比最后一个还小：并进最后一个桶（晚一点过期，不会少算）
        if buckets and buckets[-1][0] >= bucket:
            buckets[-1][1] += amount
        else:
            buckets.append([bucket, amount])
        entry[0] += amount
        while len(self._data) > self.max_keys:
            self._data.popitem(last=False)
            self.evicted += 1

    def total(self, key: str, now_ms: int) -> int:
        """窗口内合计。查一个没见过的 key 返回 0，**不会**因此建出这个 key。"""
        entry = self._data.get(key)
        if entry is None:
            return 0
        self._expire(entry, self._floor(now_ms))
        if not entry[1]:
            del self._data[key]
            return 0
        return int(entry[0])

    def retry_after_ms(self, key: str, limit: int, now_ms: int) -> int | None:
        """已用 >= limit 时，还要多久才降回 limit 以下（毫秒）。没超限返回 None。"""
        if limit <= 0:
            return None
        entry = self._data.get(key)
        if entry is None:
            return None
        now_ms = int(now_ms)
        self._expire(entry, self._floor(now_ms))
        remaining = int(entry[0])
        if remaining < limit:
            return None
        for bucket, amount in entry[1]:
            remaining -= amount
            if remaining < limit:
                # 桶号 b 在 now >= (b + 1) * bucket_ms + window_ms 时滑出窗口
                return max(0, (bucket + 1) * self.bucket_ms + self.window_ms - now_ms)
        return None

    def prune(self, now_ms: int) -> None:
        floor = self._floor(now_ms)
        for key in list(self._data):
            entry = self._data[key]
            self._expire(entry, floor)
            if not entry[1]:
                del self._data[key]

    def top(self, n: int, now_ms: int) -> list[tuple[str, int]]:
        self.prune(now_ms)
        return heapq.nlargest(n, ((k, int(e[0])) for k, e in self._data.items()), key=lambda kv: kv[1])

    def __len__(self) -> int:
        return len(self._data)


@dataclass(frozen=True, slots=True)
class Hit:
    """某个维度已经到上限。"""

    dim: str
    key: str
    used: int
    limit: int
    retry_after_ms: int | None


class UsageLedger:
    """音频（毫秒）和润色（token）两组滚动窗口。"""

    def __init__(self, *, max_keys: int = MAX_KEYS) -> None:
        self.audio: dict[str, RollingWindow] = {dim: RollingWindow(max_keys=max_keys) for dim in AUDIO_DIMS}
        self.ai: dict[str, RollingWindow] = {dim: RollingWindow(max_keys=max_keys) for dim in AI_DIMS}

    @staticmethod
    def over(
        windows: Mapping[str, RollingWindow],
        keys: Iterable[tuple[str, str]],
        limits: Mapping[str, int],
        now_ms: int,
    ) -> list[Hit]:
        """列出已经 >= 上限的维度。上限 <= 0 的维度不限。只读，不登记任何东西。"""
        hits: list[Hit] = []
        for dim, key in keys:
            limit = int(limits.get(dim, 0) or 0)
            if limit <= 0 or not key:
                continue
            window = windows[dim]
            used = window.total(key, now_ms)
            if used >= limit:
                hits.append(Hit(dim, key, used, limit, window.retry_after_ms(key, limit, now_ms)))
        return hits

    @staticmethod
    def add(windows: Mapping[str, RollingWindow], keys: Iterable[tuple[str, str]], amount: int, now_ms: int) -> None:
        for dim, key in keys:
            windows[dim].add(key, amount, now_ms)

    def evicted(self) -> int:
        return sum(w.evicted for w in self.audio.values()) + sum(w.evicted for w in self.ai.values())

    def tracked(self) -> int:
        return sum(len(w) for w in self.audio.values()) + sum(len(w) for w in self.ai.values())


# seed 只认「管线真的跑完」的会话：实时记账也只在处理完成后记。被拒、失败、空音频的都不算。
# started_at 是毫秒整数；时间下界在 Python 里算好再传进去，不用任何 SQL 日期函数（SQLite / PG 通用）。
SEED_SQL = (
    "SELECT started_at, device_id, client_ip, forwarded_for, source, audio_duration_ms "
    "FROM usage_sessions "
    "WHERE started_at >= ? AND audio_duration_ms > 0 AND status IN ('success', 'empty_result') "
    "ORDER BY started_at ASC"
)


@dataclass(frozen=True, slots=True)
class SeedResult:
    rows: int
    seeded: int
    skipped: int
    ip_unknown: int


def seed_audio_from_db(
    ledger: UsageLedger,
    db: Any,
    *,
    now_ms: int,
    keys_for_row: Callable[[dict[str, Any]], tuple[list[tuple[str, str]], bool]],
) -> SeedResult:
    """从 usage_sessions 补回最近 24 小时的音频用量。

    ``keys_for_row(row)`` 返回 ``(这一行计到哪些维度, IP 是否可信)``，由调用方用与实时连接
    **同一个**取 IP 的函数从 client_ip / forwarded_for 重算。IP 不可信的行只补设备维度。
    时间全部按整数毫秒算；一行的用量记在它的 started_at 上（实时记在处理完成时，差不到一个桶）。
    """
    now_ms = int(now_ms)
    since_ms = now_ms - WINDOW_MS
    rows = db.fetch_all(SEED_SQL, (since_ms,))
    seeded = skipped = ip_unknown = 0
    for row in rows:
        try:
            started = int(row.get("started_at") or 0)
            audio_ms = int(row.get("audio_duration_ms") or 0)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if audio_ms <= 0 or started < since_ms or started > now_ms:
            skipped += 1
            continue
        keys, ip_ok = keys_for_row(row)
        if not ip_ok:
            ip_unknown += 1
            keys = [(dim, key) for dim, key in keys if dim not in IP_DIMS and dim != DIM_INTERNAL]
        if not keys:
            skipped += 1
            continue
        UsageLedger.add(ledger.audio, keys, audio_ms, started)
        seeded += 1
    return SeedResult(rows=len(rows), seeded=seeded, skipped=skipped, ip_unknown=ip_unknown)
