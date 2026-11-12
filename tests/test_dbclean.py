#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""database/ 自动清理的测试。

背景：database/ 里全是中间缓存（最终产物是 csv_output/选股_<S>.csv），
早期只在手动加 --prune-kline 时删旧底稿，导致目录只增不减（每周 +66MB）。

回归点：
  1. kline 底稿只留最新 KLINE_KEEP 份（连同 .meta.json 成对删）；
  2. 逐日缓存（cross/labels/mv/quote/result/finance*）只留最近 KEEP_DAYS 天；
  3. 常驻文件（trading_calendar/industry_map 等）永不删；
  4. protect 指定的文件永不删（本次运行刚写的底稿）；
  5. 认不出的文件不误删（保守优先）；
  6. dry_run 只报告不删。

运行：
    python3 -m unittest tests.test_dbclean -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kline_screen.common import dbclean  # noqa: E402


class DbCleanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dbclean-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        p = mock.patch.object(dbclean, "DB", self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def touch(self, name, size=10):
        Path(self.tmp, name).write_bytes(b"x" * size)
        return os.path.join(self.tmp, name)

    def names(self):
        return set(os.listdir(self.tmp))

    def test_keeps_newest_kline_only(self):
        self.touch("kline_2026-01-08_2026-11-06.json")
        self.touch("kline_2026-01-08_2026-11-06.meta.json")
        self.touch("kline_2026-01-13_2026-11-13.json")
        self.touch("kline_2026-01-13_2026-11-13.meta.json")
        self.touch("kline_2026-01-15_2026-11-16.json")
        self.touch("kline_2026-01-15_2026-11-16.meta.json")
        dbclean.clean(keep_kline=2, quiet=True)
        left = self.names()
        self.assertIn("kline_2026-01-15_2026-11-16.json", left)
        self.assertIn("kline_2026-01-13_2026-11-13.json", left)
        self.assertNotIn("kline_2026-01-08_2026-11-06.json", left)
        # .meta.json 必须跟着走，不能留孤儿
        self.assertNotIn("kline_2026-01-08_2026-11-06.meta.json", left)
        self.assertIn("kline_2026-01-15_2026-11-16.meta.json", left)

    def test_keeps_recent_daily_drops_old(self):
        for d in ("2026-11-01", "2026-11-05", "2026-11-09", "2026-11-10"):
            self.touch(f"cross_{d}.json")
        dbclean.clean(keep_days=2, quiet=True)
        left = self.names()
        # 日期最大的 2 个保留
        self.assertIn("cross_2026-11-10.json", left)
        self.assertIn("cross_2026-11-09.json", left)
        self.assertNotIn("cross_2026-11-01.json", left)
        self.assertNotIn("cross_2026-11-05.json", left)

    def test_never_deletes_pinned_or_protected(self):
        self.touch("trading_calendar.json")
        self.touch("industry_map.json")
        self.touch("gene_industry_map.json")
        self.touch("history_meta.json")
        old = self.touch("kline_2025-08-05_2026-10-14.json")
        self.touch("kline_2026-01-13_2026-11-13.json")
        dbclean.clean(keep_kline=1, protect=[old], quiet=True)
        left = self.names()
        for n in ("trading_calendar.json", "industry_map.json",
                  "gene_industry_map.json", "history_meta.json"):
            self.assertIn(n, left)
        self.assertIn("kline_2025-08-05_2026-10-14.json", left, "protect 的不能删")

    def test_dry_run_deletes_nothing(self):
        self.touch("kline_2025-08-05_2026-10-14.json")
        self.touch("kline_2026-01-13_2026-11-13.json")
        self.touch("kline_2026-01-15_2026-11-16.json")
        before = self.names()
        n, freed = dbclean.clean(keep_kline=1, dry_run=True, quiet=True)
        self.assertEqual(n, 0)
        self.assertEqual(self.names(), before)

    def test_unknown_files_untouched(self):
        """认不出的文件名不碰 —— 宁可留着也不能误删。"""
        self.touch("some_weird_cache.json")
        self.touch("backup_2025.json")
        dbclean.clean(keep_days=1, keep_kline=1, quiet=True)
        self.assertIn("some_weird_cache.json", self.names())
        self.assertIn("backup_2025.json", self.names())

    def test_returns_freed_bytes(self):
        self.touch("kline_2025-08-05_2026-10-14.json", 1000)
        self.touch("kline_2026-01-13_2026-11-13.json", 1000)
        self.touch("kline_2026-01-15_2026-11-16.json", 1000)
        n, freed = dbclean.clean(keep_kline=1, quiet=True)
        self.assertEqual(n, 2, "3 份底稿留 1 份 → 删 2 份")
        self.assertEqual(freed, 2000)


if __name__ == "__main__":
    unittest.main()
