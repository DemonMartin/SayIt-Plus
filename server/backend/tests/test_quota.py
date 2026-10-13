"""quota：滚动窗口计数、维度划分、从 usage_sessions 补回用量（整数毫秒）。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from backend.app.db import Database
from backend.app.quota import (
    BUCKET_MS,
    DIM_DEMO_IP,
    DIM_DEVICE,
    DIM_INTERNAL,
    DIM_IP,
    DIM_NONLIVE_DEVICE,
    DIM_NONLIVE_IP,
    LANE_DEMO,
    LANE_LIVE,
    LANE_NONLIVE,
    WINDOW_MS,
    RollingWindow,
    UsageLedger,
    ai_keys,
    audio_keys,
    seed_audio_from_db,
)

T0 = 1_800_000_000_000  # 一个固定的毫秒时间戳，整除桶宽


class RollingWindowTests(unittest.TestCase):
    def test_same_bucket_accumulates(self) -> None:
        w = RollingWindow()
        w.add("k", 1000, T0)
        w.add("k", 500, T0 + 1000)
        self.assertEqual(w.total("k", T0 + 2000), 1500)

    def test_expires_after_window(self) -> None:
        w = RollingWindow()
        w.add("k", 1000, T0)
        w.add("k", 700, T0 + 6 * 60 * 60 * 1000)
        # 窗口时长之后还在最多一个桶宽的时间里（宁可晚放）
        self.assertEqual(w.total("k", T0 + WINDOW_MS), 1700)
        self.assertEqual(w.total("k", T0 + WINDOW_MS + BUCKET_MS), 700)
        self.assertEqual(w.total("k", T0 + 2 * WINDOW_MS), 0)
        self.assertEqual(len(w), 0)

    def test_total_of_unknown_key_does_not_create_it(self) -> None:
        w = RollingWindow()
        self.assertEqual(w.total("nobody", T0), 0)
        self.assertEqual(len(w), 0)

    def test_evicts_least_recently_updated(self) -> None:
        w = RollingWindow(max_keys=2)
        w.add("a", 1, T0)
        w.add("b", 1, T0)
        w.add("a", 1, T0 + 1)  # a 变成最近更新
        w.add("c", 1, T0 + 2)
        self.assertEqual(w.evicted, 1)
        self.assertEqual(w.total("b", T0 + 3), 0)
        self.assertEqual(w.total("a", T0 + 3), 2)

    def test_prune_and_top(self) -> None:
        w = RollingWindow()
        w.add("old", 9999, T0)
        w.add("x", 10, T0 + WINDOW_MS)
        w.add("y", 30, T0 + WINDOW_MS)
        w.add("z", 20, T0 + WINDOW_MS)
        self.assertEqual(w.top(2, T0 + WINDOW_MS + BUCKET_MS), [("y", 30), ("z", 20)])
        self.assertEqual(len(w), 3)  # old 已被清掉

    def test_retry_after(self) -> None:
        w = RollingWindow()
        w.add("k", 700, T0)
        w.add("k", 700, T0 + 2 * 60 * 60 * 1000)
        now = T0 + 3 * 60 * 60 * 1000
        self.assertIsNone(w.retry_after_ms("k", 2000, now))  # 没超
        # 上限 1000：第一笔滑出窗口后降到 700 < 1000
        expected = (T0 // BUCKET_MS + 1) * BUCKET_MS + WINDOW_MS - now
        self.assertEqual(w.retry_after_ms("k", 1000, now), expected)
        self.assertGreater(expected, 20 * 60 * 60 * 1000)
        # 上限 500：两笔都要滑出
        self.assertGreater(w.retry_after_ms("k", 500, now), expected)

    def test_clock_going_backwards_never_drops_usage(self) -> None:
        w = RollingWindow()
        w.add("k", 100, T0 + BUCKET_MS * 3)
        w.add("k", 50, T0)
        self.assertEqual(w.total("k", T0 + BUCKET_MS * 3), 150)


class DimensionTests(unittest.TestCase):
    def test_live(self) -> None:
        self.assertEqual(audio_keys(LANE_LIVE, device="d1", ip="198.51.100.7"), [(DIM_DEVICE, "d1"), (DIM_IP, "198.51.100.7")])
        self.assertEqual(audio_keys(LANE_LIVE, device="", ip="198.51.100.7"), [(DIM_IP, "198.51.100.7")])

    def test_nonlive_also_counts_toward_device_and_ip_totals(self) -> None:
        self.assertEqual(
            audio_keys(LANE_NONLIVE, device="d1", ip="198.51.100.7"),
            [(DIM_DEVICE, "d1"), (DIM_IP, "198.51.100.7"), (DIM_NONLIVE_DEVICE, "d1"), (DIM_NONLIVE_IP, "198.51.100.7")],
        )

    def test_demo_and_internal(self) -> None:
        # 网页体验同时计入单 IP 和全站合计
        self.assertEqual(
            audio_keys(LANE_DEMO, device="", ip="198.51.100.7"),
            [(DIM_DEMO_IP, "198.51.100.7"), ("demo_site", "site")],
        )
        self.assertEqual(
            audio_keys(LANE_NONLIVE, device="d1", ip="198.51.100.7", internal_ip="203.0.113.5"),
            [(DIM_INTERNAL, "203.0.113.5")],
        )

    def test_ai_keys(self) -> None:
        self.assertEqual(ai_keys(LANE_LIVE, device="d1", ip="198.51.100.7"), [(DIM_DEVICE, "d1"), (DIM_IP, "198.51.100.7")])
        self.assertEqual(ai_keys(LANE_DEMO, device="d1", ip="198.51.100.7"), [(DIM_IP, "198.51.100.7")])

    def test_over_only_reads(self) -> None:
        ledger = UsageLedger()
        keys = [(DIM_DEVICE, "d1"), (DIM_IP, "198.51.100.7")]
        ledger.add(ledger.audio, keys, 1000, T0)
        hits = ledger.over(ledger.audio, keys, {DIM_DEVICE: 1000, DIM_IP: 0}, T0 + 1)
        self.assertEqual([(h.dim, h.used, h.limit) for h in hits], [(DIM_DEVICE, 1000, 1000)])
        self.assertEqual(ledger.over(ledger.audio, keys, {DIM_DEVICE: 1001}, T0 + 1), [])
        self.assertEqual(ledger.audio[DIM_DEVICE].total("d1", T0 + 2), 1000)


class SeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="sayit-quota-test-"))
        self.db = Database(str(self._tmp / "t.sqlite3"))
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _row(self, sid: str, started_at: int, audio_ms: int, *, status: str = "success", device: str | None = "d1",
             source: str = "live", forwarded: str | None = "198.51.100.7, 10.0.1.23") -> None:
        self.db.execute(
            "INSERT INTO usage_sessions(session_id, connection_id, started_at, device_id, client_ip, forwarded_for,"
            " source, audio_duration_ms, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, "cid", started_at, device, "127.0.0.1", forwarded, source, audio_ms, status, started_at, started_at),
        )

    def test_seeds_last_24h_with_integer_ms(self) -> None:
        now = T0 + WINDOW_MS
        self._row("old", now - WINDOW_MS - 1, 9_000)  # 刚好在窗口外
        self._row("a", now - WINDOW_MS, 1_000)  # 下界本身算
        self._row("b", now - 1, 2_000)
        self._row("nonlive", now - 10, 4_000, source="history_reprocess")
        self._row("failed", now - 5, 8_000, status="failed")
        self._row("rejected", now - 5, 8_000, status="rejected_quota")
        self._row("empty", now - 5, 0)
        seen: list[dict] = []

        def keys_for_row(row: dict) -> tuple[list[tuple[str, str]], bool]:
            seen.append(row)
            lane = LANE_LIVE if row["source"] == "live" else LANE_NONLIVE
            return audio_keys(lane, device=row["device_id"] or "", ip="198.51.100.7"), True

        ledger = UsageLedger()
        result = seed_audio_from_db(ledger, self.db, now_ms=now, keys_for_row=keys_for_row)
        self.assertEqual((result.rows, result.seeded), (3, 3))
        self.assertTrue(all(isinstance(r["started_at"], int) for r in seen))
        self.assertEqual(ledger.audio[DIM_DEVICE].total("d1", now), 7_000)
        self.assertEqual(ledger.audio[DIM_IP].total("198.51.100.7", now), 7_000)
        self.assertEqual(ledger.audio[DIM_NONLIVE_DEVICE].total("d1", now), 4_000)
        self.assertEqual(ledger.audio[DIM_NONLIVE_IP].total("198.51.100.7", now), 4_000)
        # 记在 started_at 上：a 那笔一个桶宽之后就滑出窗口
        self.assertEqual(ledger.audio[DIM_DEVICE].total("d1", now + BUCKET_MS), 6_000)

    def test_untrusted_ip_rows_seed_device_only(self) -> None:
        now = T0 + WINDOW_MS
        self._row("a", now - 1000, 3_000)

        def keys_for_row(row: dict) -> tuple[list[tuple[str, str]], bool]:
            return audio_keys(LANE_NONLIVE, device="d1", ip="198.51.100.7"), False

        ledger = UsageLedger()
        result = seed_audio_from_db(ledger, self.db, now_ms=now, keys_for_row=keys_for_row)
        self.assertEqual(result.ip_unknown, 1)
        self.assertEqual(ledger.audio[DIM_DEVICE].total("d1", now), 3_000)
        self.assertEqual(ledger.audio[DIM_NONLIVE_DEVICE].total("d1", now), 3_000)
        self.assertEqual(len(ledger.audio[DIM_IP]), 0)
        self.assertEqual(len(ledger.audio[DIM_NONLIVE_IP]), 0)


if __name__ == "__main__":
    unittest.main()
