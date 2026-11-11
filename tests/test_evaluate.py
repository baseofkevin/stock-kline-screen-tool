#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evaluate.py 纯函数测试：不联网、不依赖 database/ 缓存。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

import evaluate as ev  # noqa: E402


class HorizonRetTest(unittest.TestCase):
    def test_exact_target(self):
        dates = ["D0", "D1", "D2", "D3"]
        closes = [10.0, 11.0, 12.0, 9.0]
        self.assertAlmostEqual(ev.horizon_ret(dates, closes, "D0", "D2"), 0.2)

    def test_missing_S(self):
        self.assertIsNone(ev.horizon_ret(["D1"], [10.0], "D0", "D1"))

    def test_target_not_traded(self):
        # 停牌：target 当天没有成交价，不能拿之前最近一根顶替
        dates = ["D0", "D1"]
        closes = [10.0, 11.0]
        self.assertIsNone(ev.horizon_ret(dates, closes, "D0", "D2"))

    def test_target_equals_S(self):
        self.assertIsNone(ev.horizon_ret(["D0", "D1"], [10.0, 11.0], "D1", "D1"))


class LimitTest(unittest.TestCase):
    def test_limit_pct(self):
        self.assertEqual(ev.limit_pct("sh600000"), 10.0)
        self.assertEqual(ev.limit_pct("sz000001"), 10.0)
        self.assertEqual(ev.limit_pct("sz300750"), 20.0)
        self.assertEqual(ev.limit_pct("sz301001"), 20.0)
        self.assertEqual(ev.limit_pct("sh688001"), 20.0)
        self.assertEqual(ev.limit_pct("bj830799"), 30.0)

    def test_limit_pct_covers_cdr_and_new_chinext_segment(self):
        """sh689（科创 CDR）与 sz302（创业板）都是 20%，漏了就会标成主板 10%。"""
        self.assertEqual(ev.limit_pct("sh689009"), 20.0)
        self.assertEqual(ev.limit_pct("sz302132"), 20.0)
        # 20% 的票涨 12% 不该被认成涨停，但涨 19.9% 必须是「是」
        self.assertEqual(ev.limit_state(12.0, "sh689009"), "否")
        self.assertEqual(ev.limit_state(19.9, "sz302132"), "是")

    def test_limit_state(self):
        self.assertEqual(ev.limit_state(9.95, "sh600000"), "是")
        self.assertEqual(ev.limit_state(9.6, "sh600000"), "接近")
        self.assertEqual(ev.limit_state(5.0, "sh600000"), "否")
        self.assertEqual(ev.limit_state(19.9, "sz300750"), "是")
        self.assertEqual(ev.limit_state(10.0, "sz300750"), "否")
        self.assertEqual(ev.limit_state(None, "sh600000"), "")


class BucketTest(unittest.TestCase):
    def test_size_bucket(self):
        cuts = [10, 20, 30, 40]
        self.assertEqual(ev.size_bucket(5, cuts), 0)
        self.assertEqual(ev.size_bucket(10, cuts), 0)
        self.assertEqual(ev.size_bucket(11, cuts), 1)
        self.assertEqual(ev.size_bucket(100, cuts), 4)
        self.assertIsNone(ev.size_bucket(None, cuts))
        self.assertIsNone(ev.size_bucket(5, []))

    def test_bucket_cuts_small_sample(self):
        self.assertEqual(ev.bucket_cuts([float(i) for i in range(10)]), [])


if __name__ == "__main__":
    unittest.main()
