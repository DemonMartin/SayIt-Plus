"""连接防护：推流检测、准入、音频额度、润色预算、白名单，以及统一的 guard 日志 / 统计 / 启动 banner。

- config.yaml 没有 guard 段时这里什么都不做：不统计、不记账、不打 netid，只多一行启动 banner。
- 每项检查 off / log / enforce 三档。log 只写 ``guard violation ...``，enforce 才拒绝或降级。
- 所有拒绝都发生在 main._run_asr_llm 之前：被拒的会话不进识别。
- 润色预算按模型接口报回的 token 记（输入 + 输出），所以 system_prompt、选区、上下文都算在内；
  system_prompt 另有长度上限。两者超了都只停润色，识别照常。
- 计数在内存里，假设只有一个 uvicorn worker（见 quota.py）。

日志一律是 ``guard <violation|reject|degrade|alert|stats> dim=... mode=... ip=... device=... src=...
cid=... sid=... detail=...``，只有长度、计数和标识，绝不记识别文本、选区或 system_prompt。
来自客户端的值进日志前都过一遍 _clean，防止换行之类把日志行拆开。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .config import GuardConfig
from .netutil import IpResolver, ip_in, legacy_ip_key, parse_ip, parse_networks
from .quota import (
    DIM_DEMO_IP,
    DIM_DEVICE,
    DIM_INTERNAL,
    DIM_IP,
    DIM_NONLIVE_DEVICE,
    DIM_NONLIVE_IP,
    IP_DIMS,
    LANE_DEMO,
    LANE_LIVE,
    LANE_NONLIVE,
    RollingWindow,
    SeedResult,
    UsageLedger,
    ai_keys,
    audio_keys,
    seed_audio_from_db,
)

logger = logging.getLogger("sayit.guard")

MODE_OFF = "off"
MODE_LOG = "log"
MODE_ENFORCE = "enforce"

# 只有脚本才会撞上的规则（准入、推流）统一回这一句：不告诉对方是哪条规则拦的。
GENERIC_REJECT_MESSAGE = "连接异常，请使用最新版 SayIt 客户端"
STATUS_REJECTED_PACE = "rejected_pace"
STATUS_REJECTED_ADMISSION = "rejected_admission"
STATUS_REJECTED_QUOTA = "rejected_quota"
LLM_SKIPPED_QUOTA = "quota"

# ⚠️ 准入格式是**暂定**的：照客户端源码写的（device_id = "sayit-" + 小写 uuid4，
# client_version = Cargo 包版本号），上 enforce 之前必须先拿 dev 库里真实的 device_id /
# client_version 核对一遍，有对不上的就改这里。fullmatch + re.ASCII：\d 不认全角/阿拉伯数字。
DEVICE_ID_PATTERN = re.compile(r"^sayit-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CLIENT_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$", re.ASCII)
_ADMISSION_MAX_LEN = 128  # 超长的直接判不合格，不跑正则

_DEVICE_KEY_MAX = 128
# telemetry 存 forwarded_for 时截到 512 字符：截断丢的是右端（可信代理那几跳），
# 这种行重算出来的 IP 不可信，seed 时只补设备维度。
_STORED_FORWARDED_MAX = 512

# 同一 (类型, 维度, IP, 设备) 60 秒内只打一行，被压掉的条数带在下一行 suppressed=N。计数不受影响。
_EVENT_DEDUPE_SEC = 60.0
_DEDUPE_MAX_KEYS = 10_000
_NETID_DEDUPE_SEC = 10 * 60.0
STATS_INTERVAL_SEC = 60 * 60
_HOUR_MS = 60 * 60 * 1000
_SITE = "site"

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._:@/+\-]")
_XFF_UNSAFE_RE = re.compile(r"[^0-9A-Fa-f.:,\[\]]")
_WS_RE = re.compile(r"\s+")


def _clean(value: Any, limit: int = 64) -> str:
    """客户端给的值进日志前：截断 + 把空格、换行、等号等换成 ?，一行日志就不会被拆开或伪造字段。"""
    text = "" if value is None else str(value)
    if not text:
        return "-"
    cut = len(text) > limit
    text = _UNSAFE_RE.sub("?", text[:limit])
    return text + "~" if cut else text


def _clean_xff(xff: Any, limit: int = 200) -> str:
    if not xff:
        return "-"
    text = _WS_RE.sub("", str(xff))
    cut = len(text) > limit
    text = _XFF_UNSAFE_RE.sub("?", text[:limit])
    return text + "~" if cut else text


def _device_key(raw: Any) -> str:
    return str(raw or "").strip()[:_DEVICE_KEY_MAX]


def _minutes_ms(minutes: float) -> int:
    return int(round(float(minutes) * 60_000)) if minutes and minutes > 0 else 0


def _fmt_min(ms: int) -> str:
    return f"{ms / 60_000:.1f}"


def _fmt_wait(ms: int | None) -> str:
    if ms is None:
        return "-"
    return f"{max(0, int(ms)) // 1000}s"


def human_wait(ms: int | None) -> str:
    """额度恢复时间的口语写法：一小时以内按分钟向上取整，再长按小时四舍五入。"""
    ms = max(0, int(ms or 0))
    minutes = max(1, -(-ms // 60_000))
    if minutes <= 60:
        return f"{minutes} 分钟"
    return f"{max(1, round(ms / 3_600_000))} 小时"


def quota_message(retry_after_ms: int | None) -> str:
    # 带上 "quota" 一词：桌面端按报错文本归类，命中它才会提示「被限流、稍后再试」，
    # 否则落到「连接失败」，用户会去查网络。
    return f"服务器识别额度已用完（quota），约 {human_wait(retry_after_ms)}后恢复"


@dataclass(frozen=True, slots=True)
class Rejection:
    """一次拒绝。payload 发给客户端；detail 只写进服务端的 usage_sessions，不发出去。"""

    status: str
    detail: str
    payload: dict
    close: bool  # 发完 error + done 之后是否以 4003 关闭连接


def _generic_rejection(status: str, detail: str) -> Rejection:
    return Rejection(
        status=status,
        detail=detail,
        payload={"type": "error", "code": "rejected", "message": GENERIC_REJECT_MESSAGE},
        close=True,
    )


def _quota_rejection(detail: str, retry_after_ms: int | None) -> Rejection:
    # 额度是正常用户也可能撞上的：说清楚原因和大概多久恢复，也不断开连接（断开会让客户端重连）。
    return Rejection(
        status=STATUS_REJECTED_QUOTA,
        detail=detail,
        payload={"type": "error", "code": "quota", "message": quota_message(retry_after_ms)},
        close=False,
    )


def _admission_rule(client_meta: Mapping[str, Any]) -> str | None:
    device = str(client_meta.get("device_id") or "")
    if len(device) > _ADMISSION_MAX_LEN or not DEVICE_ID_PATTERN.fullmatch(device):
        return "device_id"
    version = str(client_meta.get("client_version") or "")
    if len(version) > _ADMISSION_MAX_LEN or not CLIENT_VERSION_PATTERN.fullmatch(version):
        return "client_version"
    return None


@dataclass(slots=True)
class _Session:
    lane: str
    source: str
    device: str
    sid: str
    t_start: float
    compare: bool
    pace: bool = False  # 这个会话还要不要查推流
    pcm_bytes: int = 0  # 本会话已收到的音频字节数（推流按连接累计时要用）
    implicit: bool = False  # 没发 start、直接 resume 出来的会话


class _Stats:
    """一个统计周期（默认一小时）内的计数，打完 stats 行就清零。"""

    def __init__(self, started: float) -> None:
        self.started = started
        self.sessions: Counter[str] = Counter()
        self.audio_ms: Counter[str] = Counter()
        self.ai_tokens = 0
        self.events: Counter[tuple[str, str]] = Counter()
        self.alerts = 0


class _NullConnection:
    """没有 guard 段时用：所有检查放行，什么也不记。"""

    whitelisted = False

    def check_start(self, **_: Any) -> Rejection | None:
        return None

    def check_resume(self) -> Rejection | None:
        return None

    def set_sid(self, _sid: str | None) -> None:
        return None

    def check_pace(self, _pcm_bytes: int) -> Rejection | None:
        return None

    def check_stop(self, **_: Any) -> Rejection | None:
        return None

    def ai_blocked(self, **_: Any) -> bool:
        return False

    def charge_audio(self, _audio_ms: int) -> None:
        return None

    def charge_ai(self, _tokens: int) -> None:
        return None

    def end_session(self) -> None:
        return None

    def record(self, **_: Any) -> None:
        return None


_NULL_CONNECTION = _NullConnection()


class GuardConnection:
    """一条 WebSocket 连接上的防护状态。一条连接上的会话是串行的，同一时间只有一个。"""

    def __init__(self, guard: Guard, *, cid: str, ip: str, real_ip: str, is_web_demo: bool, whitelisted: bool) -> None:
        self.guard = guard
        self.cid = cid
        self.ip = ip  # 额度 / 日志用的 IP（配了可信代理就是可信 IP）
        self.real_ip = real_ip  # 按可信代理解析出来的 IP（白名单只认它）
        self.is_web_demo = is_web_demo
        self.whitelisted = whitelisted
        self.session: _Session | None = None
        # 当前会话是否通过了 start 检查、还没结束。resume 只在这时生效：不然不发 start、
        # 直接 resume 就能开始收音，start 上的所有检查全被跳过。
        self.open = False
        # 推流按连接累计：从这条连接第一次 live start 起，所有 live 会话的音频加起来也不能超过
        # 「连接时长 × 倍数 + 余量」。只按会话算的话，切成很多段短于余量的会话就能一直瞬推。
        # 真人的音频总长不可能超过连接本身的时长，所以不会误伤。
        self.live_t0: float | None = None
        self.live_done_bytes = 0

    def _audio_keys(self) -> list[tuple[str, str]]:
        s = self.session
        assert s is not None
        return audio_keys(s.lane, device=s.device, ip=self.ip, internal_ip=self.real_ip if self.whitelisted else None)

    def check_start(
        self, *, source: str, client_meta: Mapping[str, Any], compare: bool, sid: str | None, implicit: bool = False,
    ) -> Rejection | None:
        """start 时：准入（格式）+ 音频额度。同步执行，检查和登记之间没有 await。"""
        self.end_session()  # 上一个会话没走到 stop 就又 start 了：先收尾，它推过的音频照样算进连接累计
        rejection = self._check_start(source=source, client_meta=client_meta, compare=compare, sid=sid, implicit=implicit)
        self.open = rejection is None
        s = self.session
        if self.open and s is not None and s.lane == LANE_LIVE and self.live_t0 is None:
            self.live_t0 = s.t_start
        return rejection

    def check_resume(self) -> Rejection | None:
        """resume 只在已放行、还没结束的会话里生效。

        桌面端和网页体验都不发 pause / resume，不经 start 直接 resume 只可能是脚本：按准入处理
        （enforce 拒绝并断开，log 只记一行）。没被拒时补一个隐式 live 会话，推流、额度、记账照常生效。
        """
        if self.open:
            return None
        return self.check_start(source="resume", client_meta={}, compare=False, sid=None, implicit=True)

    def set_sid(self, sid: str | None) -> None:
        """start 检查要在建 telemetry 会话之前做（被拒的不建会话），放行之后再把会话号补上，日志里才对得上。"""
        if self.session is not None and sid:
            self.session.sid = sid

    def end_session(self) -> None:
        """会话结束（处理完、被拒、或被下一个 start 顶掉）。可重复调用。"""
        s = self.session
        if s is not None and self.open and s.lane == LANE_LIVE:
            self.live_done_bytes += s.pcm_bytes
        self.open = False

    def _check_start(
        self, *, source: str, client_meta: Mapping[str, Any], compare: bool, sid: str | None, implicit: bool,
    ) -> Rejection | None:
        g = self.guard
        if self.is_web_demo:
            lane = LANE_DEMO
        elif implicit or source == "live":
            lane = LANE_LIVE
        else:
            lane = LANE_NONLIVE
        if lane == LANE_DEMO:
            device = ""
        elif implicit:
            # 隐式会话没有 client_meta：沿用这条连接上一个会话的设备，按设备的额度照样生效
            device = self.session.device if self.session is not None else ""
        else:
            device = _device_key(client_meta.get("device_id"))
        self.session = s = _Session(
            lane=lane, source=source, device=device, sid=sid or "-", t_start=g._mono(), compare=bool(compare),
            implicit=implicit,
        )
        g._stats.sessions[DIM_INTERNAL if self.whitelisted else lane] += 1

        # 准入只查桌面端：网页体验不带 client_meta；白名单是自己人的脚本。
        # 不经 start 的 resume 例外：网页体验也不会这么发，一律算违规。
        if g.admission_mode != MODE_OFF and not self.whitelisted and (implicit or lane != LANE_DEMO):
            rule = "no_start" if implicit else _admission_rule(client_meta)
            if rule is not None:
                if rule == "no_start":
                    detail = "resume_without_start"
                elif rule == "device_id":
                    detail = f"len={len(str(client_meta.get('device_id') or ''))}"
                else:
                    detail = f"version={_clean(client_meta.get('client_version'), 32)}"
                if g.admission_mode == MODE_ENFORCE:
                    g._event("reject", f"admission.{rule}", self, mode=g.admission_mode, detail=detail)
                    return _generic_rejection(STATUS_REJECTED_ADMISSION, f"admission:{rule}")
                g._event("violation", f"admission.{rule}", self, mode=g.admission_mode, detail=detail)

        # 音频额度：只在 start 判「已经用掉的」，会话中途和 stop 时一律不拒
        if g.quota_mode != MODE_OFF:
            now = g.now_ms()
            blocking = []
            for hit in g.ledger.over(g.ledger.audio, self._audio_keys(), g.audio_limits, now):
                untrusted = hit.dim in IP_DIMS and not g.resolver.active
                detail = (
                    f"used={_fmt_min(hit.used)}min,limit={_fmt_min(hit.limit)}min,retry={_fmt_wait(hit.retry_after_ms)}"
                    + (",ip_untrusted" if untrusted else "")
                )
                # IP 还没核对可信（use_real_ip 未生效）时，按 IP 的维度即使 enforce 也只记录：
                # 这时的 IP 可能是网关地址（所有人一样）或可伪造的最左值，拒了会误伤一大片。
                if g.quota_mode == MODE_ENFORCE and not untrusted:
                    blocking.append(hit)
                    g._event("reject", f"quota.{hit.dim}", self, mode=g.quota_mode, detail=detail)
                else:
                    g._event("violation", f"quota.{hit.dim}", self, mode=g.quota_mode, detail=detail)
            if blocking:
                retry = max((h.retry_after_ms or 0) for h in blocking)
                return _quota_rejection("quota:" + "+".join(h.dim for h in blocking), retry)

        # 推流只查桌面端 live：历史记录重新识别、网页体验的「回放」本来就是一次性推完的
        s.pace = g.pace_mode != MODE_OFF and lane == LANE_LIVE and not self.whitelisted
        return None

    def check_pace(self, pcm_bytes: int) -> Rejection | None:
        """每收到一帧音频调一次：音频不能比实时快太多。

        两道都要过：本会话从 start 算起；整条连接从第一次 live start 算起（见 __init__ 的说明）。
        """
        s = self.session
        if s is None or not self.open:
            return None
        s.pcm_bytes = pcm_bytes
        if not s.pace:
            return None
        g = self.guard
        now = g._mono()
        # 与 main._process_stop 算 audio_duration_ms 同一口径：字节数 / 2（16bit）/ 16000（采样率）
        scope = "session"
        audio_sec = pcm_bytes / 2 / 16000
        elapsed = now - s.t_start
        allow = elapsed * g.pace_ratio + g.pace_grace
        if audio_sec <= allow and self.live_t0 is not None:
            conn_audio = (self.live_done_bytes + pcm_bytes) / 2 / 16000
            conn_elapsed = now - self.live_t0
            conn_allow = conn_elapsed * g.pace_ratio + g.pace_grace
            if conn_audio > conn_allow:
                scope, audio_sec, elapsed, allow = "connection", conn_audio, conn_elapsed, conn_allow
        if audio_sec <= allow:
            return None
        s.pace = False  # 一个会话只判一次：log 模式只留一行，enforce 只拒一次
        detail = f"scope={scope},audio={audio_sec:.1f}s,elapsed={elapsed:.1f}s,allow={allow:.1f}s"
        if g.pace_mode == MODE_ENFORCE:
            g._event("reject", "pace", self, mode=g.pace_mode, detail=detail)
            return _generic_rejection(STATUS_REJECTED_PACE, "pace")
        g._event("violation", "pace", self, mode=g.pace_mode, detail=detail)
        return None

    def check_stop(self, *, audio_ms: int, ptt_hold_ms: int | None) -> Rejection | None:
        """stop 时（识别之前）：live 会话有音频就必须带 ptt_hold_ms。"""
        s = self.session
        g = self.guard
        if s is None or g.admission_mode == MODE_OFF or s.lane != LANE_LIVE or self.whitelisted:
            return None
        if audio_ms <= 0 or ptt_hold_ms is not None:
            return None
        detail = f"audio={audio_ms / 1000:.1f}s"
        if g.admission_mode == MODE_ENFORCE:
            g._event("reject", "admission.ptt", self, mode=g.admission_mode, detail=detail)
            return _generic_rejection(STATUS_REJECTED_ADMISSION, "admission:ptt")
        g._event("violation", "admission.ptt", self, mode=g.admission_mode, detail=detail)
        return None

    def ai_blocked(self, *, prompt_chars: int = 0) -> bool:
        """润色之前查：enforce 时 system_prompt 超长、或润色用量已到上限 → True（本次只做识别）。

        长度上限对白名单不生效（自己的脚本可能带长 prompt）；用量额度对白名单照样生效：润色花的是真钱。
        """
        s = self.session
        g = self.guard
        if s is None or g.ai_mode == MODE_OFF:
            return False
        cap = g.max_prompt_chars
        if cap and prompt_chars > cap and not self.whitelisted:
            detail = f"prompt={prompt_chars},limit={cap}"
            if g.ai_mode == MODE_ENFORCE:
                g._event("degrade", "ai.prompt", self, mode=g.ai_mode, detail=detail)
                return True
            g._event("violation", "ai.prompt", self, mode=g.ai_mode, detail=detail)
        block = False
        for hit in g.ledger.over(g.ledger.ai, ai_keys(s.lane, device=s.device, ip=self.ip), g.ai_limits, g.now_ms()):
            untrusted = hit.dim in IP_DIMS and not g.resolver.active
            detail = f"used={hit.used},limit={hit.limit}" + (",ip_untrusted" if untrusted else "")
            if g.ai_mode == MODE_ENFORCE and not untrusted:
                block = True
                g._event("degrade", f"ai.{hit.dim}", self, mode=g.ai_mode, detail=detail)
            else:
                g._event("violation", f"ai.{hit.dim}", self, mode=g.ai_mode, detail=detail)
        return block

    def charge_audio(self, audio_ms: int) -> None:
        """音频记账，在送进识别**之前**调：识别一开始 GPU 就花出去了，客户端发完 stop 立刻断开也照样算。

        compare 会多跑一次识别，所以记两份。
        """
        s = self.session
        if s is None:
            return
        g = self.guard
        now = g.now_ms()
        audio = max(0, int(audio_ms)) * (2 if s.compare else 1)
        if audio:
            g.ledger.add(g.ledger.audio, self._audio_keys(), audio, now)
        g._note_usage(DIM_INTERNAL if self.whitelisted else s.lane, audio, 0, now)

    def charge_ai(self, tokens: int) -> None:
        """润色用量记账（token）：润色真的调用过之后才调。"""
        s = self.session
        if s is None:
            return
        g = self.guard
        now = g.now_ms()
        tokens = max(0, int(tokens))
        if tokens:
            g.ledger.add(g.ledger.ai, ai_keys(s.lane, device=s.device, ip=self.ip), tokens, now)
        g._note_usage(DIM_INTERNAL if self.whitelisted else s.lane, 0, tokens, now)

    def record(self, *, audio_ms: int, ai_tokens: int) -> None:
        """charge_audio + charge_ai 一起记（测试和离线补账用；实时路径分两步调）。"""
        self.charge_audio(audio_ms)
        self.charge_ai(ai_tokens)


class Guard:
    def __init__(
        self,
        cfg: GuardConfig,
        *,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.present = bool(cfg.present)
        self._clock = clock
        self._mono = mono
        self.warnings: list[str] = list(cfg.warnings)

        trusted, bad_trusted = parse_networks(cfg.trusted_proxies)
        allow, bad_allow = parse_networks(cfg.allow_ips)
        self.warnings += [f"trusted_proxies 里的 {item!r} 不是合法的 IP / CIDR，已忽略" for item in bad_trusted]
        self.warnings += [f"allow_ips 里的 {item!r} 不是合法的 IP / CIDR，已忽略" for item in bad_allow]
        self.allow = allow
        self.resolver = IpResolver(trusted, use_real_ip=cfg.use_real_ip and self.present)
        if cfg.use_real_ip and not trusted:
            self.warnings.append("use_real_ip 需要先配 trusted_proxies，本次不生效（仍按旧取法）")
        if allow and not trusted:
            self.warnings.append("trusted_proxies 为空：allow_ips 只会匹配直连对端，经代理进来的请求都匹配不上")
        for net in allow:
            for proxy in trusted:
                if net.version == proxy.version and net.overlaps(proxy):
                    self.warnings.append(
                        f"allow_ips 里的 {net} 与可信代理 {proxy} 重叠：XFF 缺失或全是可信地址的请求会被当成白名单"
                    )

        on = self.present
        self.pace_mode = cfg.pace.mode if on else MODE_OFF
        self.pace_ratio = float(cfg.pace.max_ratio)
        self.pace_grace = float(cfg.pace.grace_sec)
        if self.pace_mode != MODE_OFF and self.pace_ratio <= 0:
            # max_ratio 为 0 时 audio > grace 就违规，会把所有正常录音都判成推流
            self.warnings.append("pace.max_ratio 必须大于 0，推流检测本次不生效")
            self.pace_mode = MODE_OFF
        self.admission_mode = cfg.admission.mode if on else MODE_OFF
        self.quota_mode = cfg.quota.mode if on else MODE_OFF
        self.ai_mode = cfg.ai_budget.mode if on else MODE_OFF
        q = cfg.quota
        self.audio_limits: dict[str, int] = {
            DIM_DEVICE: _minutes_ms(q.device_minutes),
            DIM_IP: _minutes_ms(q.ip_minutes),
            DIM_NONLIVE_DEVICE: _minutes_ms(q.nonlive_device_minutes),
            DIM_NONLIVE_IP: _minutes_ms(q.nonlive_ip_minutes),
            DIM_DEMO_IP: _minutes_ms(q.demo_ip_minutes),
            DIM_INTERNAL: _minutes_ms(q.internal_minutes),
        }
        self.ai_limits: dict[str, int] = {
            DIM_DEVICE: max(0, int(cfg.ai_budget.device_tokens)),
            DIM_IP: max(0, int(cfg.ai_budget.ip_tokens)),
        }
        self.max_prompt_chars = max(0, int(cfg.ai_budget.max_system_prompt_chars))
        if self.quota_mode != MODE_OFF and not any(self.audio_limits.values()):
            self.warnings.append("quota 的上限全是 0（不限），开了也不会拦任何会话")
        if self.ai_mode != MODE_OFF and not any(self.ai_limits.values()) and not self.max_prompt_chars:
            self.warnings.append("ai_budget 的上限全是 0（不限），开了也不会停任何润色")

        self.ledger = UsageLedger()
        self.seed_result: SeedResult | None = None
        self._stats = _Stats(mono())
        # 全站最近一小时：给告警和「最忙一小时」用
        self._hour = {
            "audio": RollingWindow(window_ms=_HOUR_MS, bucket_ms=60_000, max_keys=1),
            "ai": RollingWindow(window_ms=_HOUR_MS, bucket_ms=60_000, max_keys=1),
            "hits": RollingWindow(window_ms=_HOUR_MS, bucket_ms=60_000, max_keys=1),
        }
        self._peak = {"audio": 0, "ai": 0}
        a = cfg.alert
        self._alert_threshold = {
            "audio": _minutes_ms(a.audio_minutes_per_hour),
            "ai": max(0, int(a.ai_tokens_per_hour)),
            "hits": max(0, int(a.hits_per_hour)),
        }
        self._alert_last: dict[str, int] = {}
        self._recent: dict[tuple[str, str, str, str], list[float]] = {}
        self._netid_seen: dict[tuple[str, str, str], float] = {}
        self._task: asyncio.Task[None] | None = None

    # ── 基础 ──────────────────────────────────────────────────────────

    def now_ms(self) -> int:
        return int(self._clock() * 1000)

    def ip_for(self, peer: str | None, xff: str | None, forwarded: str | None) -> tuple[str, str]:
        """(额度 / 日志用的 IP, 可信 IP)。实时连接和 seed_from_db 用的是同一个函数。

        配了可信代理就用可信 IP（哪怕 use_real_ip 还没打开，日志和统计在核对期就是准的）；
        没配就只能用旧取法。可信 IP 永远按可信代理算，白名单只认它。
        """
        real = self.resolver.real_ip(peer, xff)
        return (real if self.resolver.configured else legacy_ip_key(peer, forwarded)), real

    def connection(
        self, *, cid: str, peer: str | None, forwarded: str | None, xff: str | None, is_web_demo: bool,
    ) -> GuardConnection | _NullConnection:
        if not self.present:
            return _NULL_CONNECTION
        ip, real = self.ip_for(peer, xff, forwarded)
        whitelisted = ip_in(real, self.allow)
        self._netid(entry="ws_demo" if is_web_demo else "ws", peer=peer, xff=xff, legacy=legacy_ip_key(peer, forwarded), real=real)
        return GuardConnection(self, cid=cid, ip=ip, real_ip=real, is_web_demo=is_web_demo, whitelisted=whitelisted)

    # ── 日志 ──────────────────────────────────────────────────────────

    def _event(self, kind: str, dim: str, conn: GuardConnection, *, mode: str, detail: str) -> None:
        self._stats.events[(kind, dim.split(".", 1)[0])] += 1
        now_ms = self.now_ms()
        self._hour["hits"].add(_SITE, 1, now_ms)
        s = conn.session
        device = s.device if s else ""
        key = (kind, dim, conn.ip, device)
        mono = self._mono()
        rec = self._recent.get(key)
        if rec is not None and mono - rec[0] < _EVENT_DEDUPE_SEC:
            rec[1] += 1
        else:
            suppressed = int(rec[1]) if rec is not None else 0
            if len(self._recent) >= _DEDUPE_MAX_KEYS:
                self._recent.clear()
            self._recent[key] = [mono, 0]
            if conn.is_web_demo:
                src = "web_demo"
            else:
                src = _clean(s.source, 32) if s else "-"
            logger.info(
                "guard %s dim=%s mode=%s ip=%s device=%s src=%s cid=%s sid=%s detail=%s%s",
                kind, dim, mode, _clean(conn.ip), _clean(device, 48), src, _clean(conn.cid),
                _clean(s.sid if s else None), detail,
                f" suppressed={suppressed}" if suppressed else "",
            )
        self._check_alerts(now_ms)

    def _netid(self, *, entry: str, peer: str | None, xff: str | None, legacy: str, real: str) -> None:
        """核对可信 IP 用：同一个真实 IP 10 分钟一行。

        去重 key 里还带了对端和最近一跳代理：同一台测试机先后从不同入口（不同负载均衡）进来时
        各留一行，才能逐个入口核对；最近一跳只在对端可信时取（那是我们自己的代理写的，客户端改不了）。
        """
        if not self.cfg.netid_log:
            return
        peer_ip = parse_ip(peer)
        trusted_peer = peer_ip is not None and ip_in(peer_ip, self.resolver.trusted)
        nearest = ""
        if trusted_peer and xff:
            parts = [p for p in re.split(r"[\s,]+", xff) if p]
            nearest = parts[-1][:64] if parts else ""
        key = (real, str(peer_ip) if peer_ip is not None else str(peer), nearest)
        now = self._mono()
        last = self._netid_seen.get(key)
        if last is not None and now - last < _NETID_DEDUPE_SEC:
            return
        if len(self._netid_seen) >= _DEDUPE_MAX_KEYS:
            self._netid_seen.clear()
        self._netid_seen[key] = now
        logger.info(
            "guard netid ip=%s peer=%s xff=%s legacy=%s entry=%s trusted_peer=%s use_real_ip=%s",
            _clean(real), _clean(peer), _clean_xff(xff), _clean(legacy), entry,
            "yes" if trusted_peer else "no",
            "active" if self.resolver.active else ("ignored" if self.cfg.use_real_ip else "off"),
        )

    def _note_usage(self, lane: str, audio_ms: int, ai_tokens: int, now_ms: int) -> None:
        self._stats.audio_ms[lane] += audio_ms
        self._stats.ai_tokens += ai_tokens
        if audio_ms:
            self._hour["audio"].add(_SITE, audio_ms, now_ms)
        if ai_tokens:
            self._hour["ai"].add(_SITE, ai_tokens, now_ms)
        self._peak["audio"] = max(self._peak["audio"], self._hour["audio"].total(_SITE, now_ms))
        self._peak["ai"] = max(self._peak["ai"], self._hour["ai"].total(_SITE, now_ms))
        self._check_alerts(now_ms)

    def _check_alerts(self, now_ms: int) -> None:
        for name, threshold in self._alert_threshold.items():
            if threshold <= 0:
                continue
            value = self._hour[name].total(_SITE, now_ms)
            if value < threshold:
                continue
            last = self._alert_last.get(name)
            if last is not None and now_ms - last < _HOUR_MS:
                continue
            self._alert_last[name] = now_ms
            self._stats.alerts += 1
            if name == "audio":
                detail = f"last_hour={_fmt_min(value)}min,threshold={_fmt_min(threshold)}min"
            else:
                detail = f"last_hour={value},threshold={threshold}"
            logger.warning(
                "guard alert dim=alert.%s mode=- ip=- device=- src=- cid=- sid=- detail=%s", name, detail,
            )

    # ── 启动 ──────────────────────────────────────────────────────────

    def seed_from_db(self, db: Any) -> SeedResult | None:
        """启动时从 usage_sessions 补回最近 24 小时的音频用量（润色用量库里没有，从 0 开始）。

        库里存的 client_ip / forwarded_for 有几处与实时连接对不齐，都按「宁可少算」处理：
        - forwarded_for 只存了第一个 X-Forwarded-For 头；没有 XFF 时存的是 X-Real-IP，
          重算时会被当成 XFF（实时取法不认 X-Real-IP）。
        - forwarded_for 截到 512 字符、client_ip 截到 128：截断丢的是可信代理那几跳，
          这种行只补设备维度。
        - 库里没有「网页体验」的标记：source=live 且没有 device_id 的行（网页体验，或没带
          client_meta 的桌面端）按桌面端计进 IP 总额，demo_ip 不补。
        - compare 多跑的那一遍没有记录，只补一份。
        """
        if not self.present or db is None:
            return None

        def keys_for_row(row: dict[str, Any]) -> tuple[list[tuple[str, str]], bool]:
            forwarded = row.get("forwarded_for")
            peer = row.get("client_ip")
            ip, real = self.ip_for(peer, forwarded, forwarded)
            internal = ip_in(real, self.allow)
            source = str(row.get("source") or "live").strip()
            lane = LANE_LIVE if source == "live" else LANE_NONLIVE
            keys = audio_keys(lane, device=_device_key(row.get("device_id")), ip=ip, internal_ip=real if internal else None)
            ip_ok = not (isinstance(forwarded, str) and len(forwarded) >= _STORED_FORWARDED_MAX)
            return keys, ip_ok

        try:
            self.seed_result = seed_audio_from_db(self.ledger, db, now_ms=self.now_ms(), keys_for_row=keys_for_row)
        except Exception as exc:  # 起不来比少算严重得多
            logger.warning("guard seed failed (%s); counters start from zero", type(exc).__name__)
            self.seed_result = None
        return self.seed_result

    def banner_line(self) -> str:
        if not self.present:
            return "guard banner configured=no pace=off admission=off quota=off ai_budget=off use_real_ip=off netid_log=off"
        parts = ["guard banner configured=yes"]
        pace = self.pace_mode
        if pace != MODE_OFF:
            pace += f"(max_ratio={self.pace_ratio:g},grace_sec={self.pace_grace:g})"
        parts.append(f"pace={pace}")
        parts.append(f"admission={self.admission_mode}")
        quota = self.quota_mode
        if quota != MODE_OFF:
            quota += "(" + ",".join(f"{dim}={_fmt_min(ms)}min" for dim, ms in self.audio_limits.items() if ms) + ")"
        parts.append(f"quota={quota}")
        ai = self.ai_mode
        if ai != MODE_OFF:
            items = [f"{dim}={n}tok" for dim, n in self.ai_limits.items() if n]
            if self.max_prompt_chars:
                items.append(f"max_prompt={self.max_prompt_chars}chars")
            ai += "(" + ",".join(items) + ")"
        parts.append(f"ai_budget={ai}")
        if self.resolver.active:
            real_ip = "on"
        else:
            real_ip = "ignored" if self.cfg.use_real_ip else "off"
        parts.append(f"use_real_ip={real_ip}")
        parts.append(f"trusted_proxies={len(self.resolver.trusted)} allow_ips={len(self.allow)}")
        parts.append(f"netid_log={'on' if self.cfg.netid_log else 'off'}")
        if MODE_ENFORCE in (self.quota_mode, self.ai_mode) and not self.resolver.active:
            parts.append("ip_dims=log_only")
        alerts = ",".join(f"{k}={v}" for k, v in self._alert_threshold.items() if v)
        parts.append(f"alert={alerts or 'off'}")
        parts.append("counters=memory,single_worker")
        if self.seed_result is not None:
            parts.append(f"seeded={self.seed_result.seeded}/{self.seed_result.rows}")
        return " ".join(parts)

    def log_banner(self) -> None:
        logger.info(self.banner_line())
        for warning in self.warnings:
            logger.warning("guard config warning: %s", warning)

    # ── 统计 ──────────────────────────────────────────────────────────

    def stats_line(self, *, reset: bool = True) -> str:
        now_ms = self.now_ms()
        st = self._stats
        period = max(0, int(self._mono() - st.started))
        lanes = (LANE_LIVE, LANE_NONLIVE, LANE_DEMO, DIM_INTERNAL)
        sessions = ",".join(f"{lane}:{st.sessions.get(lane, 0)}" for lane in lanes)
        audio = ",".join(f"{lane}:{_fmt_min(st.audio_ms.get(lane, 0))}" for lane in lanes)

        def events(kind: str) -> str:
            items = sorted((check, n) for (k, check), n in st.events.items() if k == kind)
            return ",".join(f"{check}:{n}" for check, n in items) or "0"

        def top(windows: Mapping[str, RollingWindow], minutes: bool) -> str:
            out = []
            for dim, window in windows.items():
                best = window.top(1, now_ms)
                if not best:
                    continue
                key, value = best[0]
                shown = f"{_fmt_min(value)}min" if minutes else str(value)
                out.append(f"{dim}:{shown}@{_clean(key, 48)}")
            return ",".join(out) or "-"

        line = (
            f"guard stats period={period}s sessions={sessions} audio_min={audio} ai_tokens={st.ai_tokens} "
            f"violation={events('violation')} reject={events('reject')} degrade={events('degrade')} alert={st.alerts} "
            f"max24h_audio={top(self.ledger.audio, True)} max24h_ai={top(self.ledger.ai, False)} "
            f"peak_hour=audio:{_fmt_min(self._peak['audio'])}min,ai:{self._peak['ai']} "
            f"tracked={self.ledger.tracked()} evicted={self.ledger.evicted()}"
        )
        if reset:
            self._stats = _Stats(self._mono())
            self._recent.clear()
        return line

    def log_stats(self) -> None:
        logger.info(self.stats_line())

    def start_stats_task(self) -> None:
        if not self.present or self._task is not None:
            return
        self._task = asyncio.get_running_loop().create_task(self._stats_loop(), name="guard-stats")

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL_SEC)
            try:
                self.log_stats()
            except Exception:
                logger.exception("guard stats failed")

    async def stop_stats_task(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
