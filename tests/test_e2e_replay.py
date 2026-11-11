#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端离线回放测试：用 database/ 缓存重放一次完整选股流程。

设计约束：
  · 不联网——所有取数函数替换为「只读本地缓存」；
  · 不写仓库——CSV / result JSON 写入临时目录，跑完即删；
  · 缓存缺失时整类 skip（新克隆的仓库没有 database/，不应因此失败）；
  · 每个用例结束恢复被替换的模块属性，不污染其他测试。

覆盖：
  1. 正常流程（CSV 16 列、result JSON 字段、BOM/CRLF、计数自洽、总市值对账）；
  2. 换手硬过滤（--turn-pct 0.4）；
  3. 财务取数整体失败时的降级；
  4. 粗筛池为空时只写表头；
  5. 非法参数按 CliError 拒绝；
  6. CSV 不变量（排序、星级、量能、涨幅、新鲜度）。
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kline_screen import annotate, pipeline, selection  # noqa: E402
from kline_screen.metrics import turnaround  # noqa: E402
from kline_screen.output import csv_row  # noqa: E402

DB = ROOT / "database"
S = "2026-11-06"          # 有完整缓存（横截面/标签/K线/市值/行情/财报）的基准日
TMP_PREFIX = "kline-e2e-"


def _load(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _required_files(days):
    names = [
        "trading_calendar.json",
        f"mv_{S}.json",
        f"quote_{S}.json",
        f"finance_{S}.json",
        f"finance_ratio_{S}.json",
        "industry_map.json",
    ]
    names += [f"cross_{d}.json" for d in days]
    names += [f"labels_{d}.json" for d in days]
    return [DB / n for n in names]


def pick_kline_file(day=S):
    """挑一份覆盖 day 的 kline 底稿（不写死文件名）。

    底稿名随时间滚动（kline_<起>_<止>.json 每期一份），写死文件名会让清理旧
    底稿后的回放静默 skip —— skip 不等于通过。

    选择顺序：截止日 == day 优先（就是当天跑出来的底稿，结果可复现）；
    同截止日有多份时按写入时间取最新（旧代码写的底稿会少票）；
    没有正好的才退到「截止日 > day」的最新一份。
    """
    cands = []
    for path in DB.glob("kline_*_*.json"):
        m = re.match(r"kline_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$", path.name)
        if not m or m.group(2) < day:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cands.append((m.group(2) != day, -mtime, path))
    cands.sort()
    return cands[0][2] if cands else None


class ReplayPipelineTest(unittest.TestCase):
    """用 2026-11-06 的缓存重放完整选股流程。"""

    @classmethod
    def setUpClass(cls):
        cal = _load(DB / "trading_calendar.json", {})
        days = [d for d in cal.get("days", []) if d <= S]
        if not days:
            raise unittest.SkipTest("缺少 trading_calendar.json 或其中无 2026-11-06 之前的数据")
        need = days[-5:]                      # 默认 n=4 → 覆盖 5 个交易日
        missing = [p.name for p in _required_files(need) if not p.exists()]
        if missing:
            raise unittest.SkipTest(
                "缺少端到端回放所需缓存：" + "、".join(missing[:6])
                + ("…" if len(missing) > 6 else ""))
        kline = pick_kline_file(S)
        if kline is None:
            raise unittest.SkipTest(f"没有覆盖 {S} 的 kline_*.json 底稿")
        cls.KLINE_FILE = kline
        cls.DAYS = days
        cls.NEED = need
        cls.CROSS = {d: _load(DB / f"cross_{d}.json", []) for d in need}
        cls.LABELS = {d: _load(DB / f"labels_{d}.json", {"st": [], "new": []})
                      for d in need}
        cls.KL = _load(cls.KLINE_FILE, {})
        cls.MV = _load(DB / f"mv_{S}.json", {})
        cls.FIN = _load(DB / f"finance_{S}.json", {})
        cls.RAT = _load(DB / f"finance_ratio_{S}.json", {})
        cls.QUOTE = _load(DB / f"quote_{S}.json", {})
        cls.IND = _load(DB / "industry_map.json", {})

    # ---------------- 环境搭建 ----------------

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = []
        self.empty_pool = False
        self._install_patches()

    def tearDown(self):
        while self._saved:
            obj, name, val = self._saved.pop()
            setattr(obj, name, val)

    def _set(self, obj, name, val):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def _install_patches(self):
        db, out = str(self.tmp / "db"), str(self.tmp / "csv")
        self._set(pipeline, "DB", db)
        self._set(pipeline, "CSVDIR", out)
        self._set(annotate, "DB", db)
        self._set(annotate, "CSVDIR", out)

        self._set(pipeline, "get_trading_days",
                  lambda end, refresh, span=None: [d for d in self.DAYS if d <= end])
        self._set(pipeline, "get_cross", self._fake_cross)
        self._set(pipeline, "get_labels",
                  lambda day, refresh: self.LABELS.get(day, {"st": [], "new": []}))
        self._set(pipeline, "get_kline", self._fake_kline)
        self._set(selection, "get_quotes", self._fake_quotes)
        self._set(selection, "get_mv", lambda day, refresh: self.MV)
        self._set(turnaround, "get_finance", self._fake_finance)
        self._set(turnaround, "get_finance_stmt", self._fake_finance_stmt)
        self._set(annotate, "get_industry_map",
                  lambda refresh=False: {"sw1": self.IND.get("sw1", {}),
                                         "sw2": self.IND.get("sw2", {})})

    # ---------------- 假取数 ----------------

    def _fake_cross(self, day, lo, hi, refresh):
        return [] if self.empty_pool else self.CROSS.get(day, [])

    def _fake_kline(self, codes, beg, end, refresh, require_codes=None):
        km, missing = {}, []
        for c in codes:
            bars = self.KL.get(c)
            if bars:
                bars = [b for b in bars if b[0] <= end]
                if bars:
                    km[c] = bars
            if c not in km:
                missing.append(c)
        return km, missing

    def _fake_quotes(self, codes, day, refresh):
        out = {}
        for c in codes:
            row = self.QUOTE.get(c)
            if row:
                out[c] = dict(row)
                continue
            # 行情缓存只覆盖上次入选名单：用 K 线收盘 + 市值补一份。
            # 取 ≤ day 的最后一根，底稿可能比 day 更新（不能直接 bars[-1]）。
            bars = [b for b in (self.KL.get(c) or []) if b[0] <= day]
            cap = (self.MV.get(c) or [None])[0]
            if bars and cap:
                out[c] = {"code": c, "price": str(bars[-1][1]),
                          "circulating_market_cap": str(cap),
                          "total_market_cap": str(cap)}
        return out

    def _fake_finance(self, codes, day, refresh):
        got = {c: self.FIN[c] for c in codes if c in self.FIN}
        return got, [c for c in codes if c not in got]

    def _fake_finance_stmt(self, codes, day, refresh, typ, fields, cache_name):
        self.assertEqual(typ, "balancesheet", "v1.9 不应再请求资产负债表")
        got = {c: self.RAT[c] for c in codes if c in self.RAT}
        return got, [c for c in codes if c not in got]

    # ---------------- 运行 ----------------

    def _run(self, **kw):
        args = dict(n=4, date=S, k_vol=1.5, chg_lo=0.5, chg_hi=20.5,
                    turn_pct=0.0, turn_min=5, turn_max=30, ma60_gap=0.0,
                    refresh=False, refresh_industry=False)
        args.update(kw)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = pipeline.run(argparse.Namespace(**args))
        date = args["date"]
        return (code,
                self.tmp / "csv" / f"选股_{date}.csv",
                self.tmp / "db" / f"result_{date}.json")

    @staticmethod
    def _read_csv(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.reader(f))

    def _result(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    # ---------------- 用例 ----------------

    HEADER = ["代码", "名称", "一级行业", "二级行业", "启动日", "距启动交易日",
              "启动日量能倍数", "启动日涨幅%", "距MA20%", "距MA60%",
              "毛利率改善", "20日换手%", "池内换手分位", "分位星级", "最新收盘价",
              "总市值（亿）"]

    def test_normal_flow(self):
        """完整流程：16 列 CSV、result v1.10、字段已清理、计数自洽。"""
        code, csv_path, res_path = self._run()
        self.assertEqual(code, 0)
        rows = self._read_csv(csv_path)
        header, body = rows[0], rows[1:]
        self.assertEqual(header, self.HEADER)
        self.assertTrue(csv_path.exists() and res_path.exists())
        self.assertGreaterEqual(len(body), 1, "缓存日无入选名单，无法验证行级断言")

        for r in body:
            with self.subTest(code=r[0]):
                self.assertEqual(len(r), 16)
                self.assertTrue(r[0].startswith(("sh", "sz", "bj")))
                self.assertEqual(len(r[4]), 10)          # 启动日 YYYY-MM-DD
                self.assertLessEqual(int(r[5]), 4)       # 新鲜度
                self.assertNotEqual(r[10], "")           # 毛利率列非空
                float(r[14])                             # 收盘价可转数字
                self.assertNotEqual(r[15], "")           # 总市值非空
                self.assertGreater(float(r[15]), 0)      # 且为正数

        res = self._result(res_path)
        self.assertEqual(res["版本"], "v1.10")
        self.assertNotIn("财务健康参数", res)
        self.assertIn("总市值列", res["参数"])   # 口径说明须可追溯
        self.assertFalse(any(k.startswith(("②", "③"))
                             for k in res["业绩转变参数"]))
        self.assertIn("①最新单季毛利率高于上年同季", res["业绩转变参数"])
        self.assertEqual(res["粗筛池"],
                         res["入选数"] + sum(res["剔除统计"].values()))
        self.assertEqual(len(body), res["入选数"])

        # 总市值对账：CSV 第 16 列 = mv 缓存的总市值(元)/1e8（回放里 get_mv
        # 对所有 d* 都返回同一份 MV 快照，因此可直接按代码对账）
        by_code = {r[0]: r for r in body}
        for x in res["名单"]:
            with self.subTest(mv=x["code"]):
                pair = self.MV.get(x["code"])
                self.assertIsNotNone(pair, f"mv 缓存缺 {x['code']}")
                expected = round(float(pair[1]) / 1e8, 2)
                self.assertAlmostEqual(float(by_code[x["code"]][15]),
                                       expected, places=6)

        for x in res["名单"]:
            with self.subTest(code=x["code"]):
                for gone in ("turn_p2", "turn_p3", "health_text",
                             "health_label", "pe", "pb", "ps"):
                    self.assertNotIn(gone, x)
                self.assertIn("turn_p1", x)
                self.assertIsNotNone(x.get("price"))

        raw = csv_path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "CSV 缺 UTF-8 BOM")
        self.assertIn(b"\r\n", raw, "CSV 不是 CRLF 换行")

        dist = {}
        for x in res["名单"]:
            v = x.get("turn_p1") or "无数据"
            dist[v] = dist.get(v, 0) + 1
        self.assertLessEqual(set(dist), {"是", "否", "无数据", "不适用"})

    def test_hard_turnover_filter(self):
        """--turn-pct 0.4：启用硬过滤，保留股换手不超过剔除线。"""
        code, csv_path, res_path = self._run(turn_pct=0.4)
        self.assertEqual(code, 0)
        body = self._read_csv(csv_path)[1:]
        res = self._result(res_path)
        meta = res["参数"]["换手过滤"]
        self.assertTrue(meta["启用"], "换手过滤未启用")
        self.assertLessEqual(len(body), res["入选数"])
        cut = meta["剔除线%"]        # 20 日换手% 阈值，不是分位
        for r in body:
            if r[11]:
                self.assertLessEqual(float(r[11]), cut + 1e-6)

    def test_finance_failure_degrades(self):
        """财务取数整体失败：仍产出名单，毛利率列留空，流程不中断。"""
        self._set(annotate, "compute_turnaround",
                  lambda codes, day, refresh: (None, list(codes)))
        code, csv_path, res_path = self._run()
        self.assertEqual(code, 0)
        body = self._read_csv(csv_path)[1:]
        self.assertTrue(body, "降级路径不应丢名单")
        self.assertTrue(all(r[10] == "" for r in body))
        res = self._result(res_path)
        self.assertFalse(res["业绩转变参数"]["数据是否可用"])

    def test_empty_pool_writes_header_only(self):
        """粗筛池为空：只写表头，计数为 0，不抛异常。"""
        self.empty_pool = True
        code, csv_path, res_path = self._run()
        self.assertEqual(code, 0)
        rows = self._read_csv(csv_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], self.HEADER)
        res = self._result(res_path)
        self.assertEqual((res["粗筛池"], res["入选数"]), (0, 0))

    def test_invalid_arguments_rejected(self):
        """非法日期/参数一律按 CliError 拒绝，不能静默产出空名单。"""
        self.empty_pool = True
        cases = {
            "非交易日": dict(date="2026-11-07"),
            "--n 为负": dict(n=-1),
            "--turn-pct 越界": dict(turn_pct=1.5),
            "--turn-pct 等于 1": dict(turn_pct=1.0),
            "涨幅区间反转": dict(chg_lo=21.0, chg_hi=1.0),
        }
        for name, kw in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(pipeline.CliError) as ctx:
                    self._run(**kw)
                self.assertIn("错误", str(ctx.exception))

    def test_csv_invariants(self):
        """排序（分位升序、空分位最后）、星级与分位一致、规则边界。"""
        code, csv_path, res_path = self._run()
        self.assertEqual(code, 0)
        res = self._result(res_path)
        rows = res["名单"]
        if not rows:
            self.skipTest("缓存日无入选名单")

        prev, seen_none = -1e9, False
        for x in rows:
            r = x.get("turn20_rank")
            if r is None:
                seen_none = True
                continue
            self.assertFalse(seen_none, "空分位排在了有分位的股票之前")
            self.assertGreaterEqual(r, prev - 1e-9, "名单未按分位升序")
            prev = r

        for x in rows:
            with self.subTest(code=x["code"]):
                r = x.get("turn20_rank")
                expect = ""
                if r is not None:
                    expect = "***" if r < 20 else "**" if r < 40 \
                        else "*" if r < 60 else ""
                self.assertEqual(csv_row(x)[13], expect)
                self.assertGreaterEqual(x["vcross"], 1.5 - 1e-6)
                self.assertTrue(0.5 - 1e-6 <= x["chg"] <= 20.5 + 1e-6)
                self.assertTrue(0 <= x["age"] <= 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
