"""guard 配置：没有 guard 段 = 全关；写错任何一项只记警告、按关处理，绝不抛异常。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from backend.app.config import (
    GuardAiBudgetConfig,
    GuardAlertConfig,
    GuardConfig,
    GuardPaceConfig,
    GuardQuotaConfig,
    load_config,
)

_EXAMPLE = Path(__file__).resolve().parents[2] / "config.example.yaml"


class GuardConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="sayit-guard-config-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _load(self, text: str) -> GuardConfig:
        path = self._tmp / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return load_config(config_path=str(path)).guard

    def assertAllOff(self, g: GuardConfig) -> None:
        self.assertEqual(
            (g.pace.mode, g.admission.mode, g.quota.mode, g.ai_budget.mode, g.web_demo.mode),
            ("off", "off", "off", "off", "off"),
        )
        self.assertFalse(g.use_real_ip)
        self.assertFalse(g.netid_log)

    def test_missing_section_is_all_off(self) -> None:
        g = self._load("server:\n  port: 8000\n")
        self.assertFalse(g.present)
        self.assertAllOff(g)
        self.assertEqual((g.trusted_proxies, g.allow_ips, g.warnings), ([], [], []))

    def test_empty_value_counts_as_missing(self) -> None:
        self.assertFalse(self._load("guard:\n").present)

    def test_empty_mapping_is_present_but_off(self) -> None:
        g = self._load("guard: {}\n")
        self.assertTrue(g.present)
        self.assertAllOff(g)
        self.assertEqual(g.warnings, [])

    def test_code_defaults_are_inert(self) -> None:
        # 代码里的默认值不能带任何推荐阈值：全部 0 / 空 / off
        g = GuardConfig()
        self.assertFalse(g.present)
        self.assertAllOff(g)
        for dc in (GuardPaceConfig(), GuardQuotaConfig(), GuardAiBudgetConfig(), GuardAlertConfig()):
            for f in fields(dc):
                if f.name == "mode":
                    continue
                with self.subTest(cls=type(dc).__name__, field=f.name):
                    self.assertEqual(getattr(dc, f.name), 0)
        self.assertEqual(g.web_demo.allowed_origins, [])

    def test_rate_and_web_demo_keys_load(self) -> None:
        text = (
            "guard:\n"
            "  quota: {mode: \"enforce\", device_sessions_per_minute: 7, demo_ip_sessions_per_hour: 3, demo_global_minutes: 2.5}\n"
            "  web_demo: {mode: \"log\", allowed_origins: [\"https://a.example\", 5]}\n"
        )
        g = self._load(text)
        self.assertEqual((g.quota.device_sessions_per_minute, g.quota.demo_ip_sessions_per_hour), (7, 3))
        self.assertEqual(g.quota.demo_global_minutes, 2.5)
        self.assertEqual((g.web_demo.mode, g.web_demo.allowed_origins), ("log", ["https://a.example"]))
        self.assertEqual(len(g.warnings), 1)
        self.assertIn("guard.web_demo.allowed_origins", g.warnings[0])

    def test_modes(self) -> None:
        text = (
            "guard:\n"
            "  pace: {mode: off}\n"         # YAML 读成 False → off，不报警告
            "  admission: {mode: LOG}\n"
            "  quota: {mode: on}\n"         # YAML 读成 True → off + 警告
            "  ai_budget: {mode: enforced}\n"
        )
        g = self._load(text)
        self.assertEqual((g.pace.mode, g.admission.mode, g.quota.mode, g.ai_budget.mode), ("off", "log", "off", "off"))
        self.assertEqual(len(g.warnings), 2)
        self.assertTrue(any("quota.mode" in w for w in g.warnings))
        self.assertTrue(any("ai_budget.mode" in w for w in g.warnings))

    def test_bad_numbers_become_zero(self) -> None:
        text = (
            "guard:\n"
            "  pace: {mode: \"log\", max_ratio: -1, grace_sec: \"3\"}\n"
            "  quota: {mode: \"log\", device_minutes: 0.5, ip_minutes: .nan, demo_ip_minutes: true}\n"
            "  ai_budget: {device_tokens: 12.7}\n"
        )
        g = self._load(text)
        self.assertEqual((g.pace.max_ratio, g.pace.grace_sec), (0.0, 0.0))
        self.assertEqual((g.quota.device_minutes, g.quota.ip_minutes, g.quota.demo_ip_minutes), (0.5, 0.0, 0.0))
        self.assertEqual(g.ai_budget.device_tokens, 12)
        self.assertEqual(len(g.warnings), 4)

    def test_lists_and_switches(self) -> None:
        text = (
            "guard:\n"
            "  trusted_proxies: \"127.0.0.1/32\"\n"
            "  allow_ips: [\"203.0.113.5\", 42, \"\"]\n"
            "  use_real_ip: \"yes\"\n"
            "  netid_log: true\n"
        )
        g = self._load(text)
        self.assertEqual(g.trusted_proxies, [])
        self.assertEqual(g.allow_ips, ["203.0.113.5"])
        self.assertFalse(g.use_real_ip)
        self.assertTrue(g.netid_log)
        self.assertEqual(len(g.warnings), 4)

    def test_unknown_keys_and_wrong_shapes_warn(self) -> None:
        g = self._load("guard:\n  use_realip: true\n  pace: [1, 2]\n  quota: {mode: \"log\", devce_minutes: 1}\n")
        self.assertTrue(g.present)
        self.assertEqual(g.pace.mode, "off")
        self.assertEqual(g.quota.mode, "log")
        joined = " ".join(g.warnings)
        for needle in ("use_realip", "guard.pace", "devce_minutes"):
            with self.subTest(needle=needle):
                self.assertIn(needle, joined)

    def test_non_mapping_section_is_ignored(self) -> None:
        g = self._load("guard: [1, 2]\n")
        self.assertFalse(g.present)
        self.assertAllOff(g)
        self.assertEqual(len(g.warnings), 1)

    def test_public_example_is_all_off(self) -> None:
        g = load_config(config_path=str(_EXAMPLE)).guard
        self.assertTrue(g.present)
        self.assertAllOff(g)
        self.assertEqual(g.allow_ips, [])
        self.assertEqual(g.trusted_proxies, ["127.0.0.1/32", "::1/128"])
        self.assertEqual(g.warnings, [])


if __name__ == "__main__":
    unittest.main()
