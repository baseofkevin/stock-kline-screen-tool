#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选股与下载的核心纯函数测试：不联网、不依赖 database/ 缓存。

运行：
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

import download_history as dh  # noqa: E402
from kline_screen import params as p  # noqa: E402
from kline_screen.common import storage as store  # noqa: E402
from kline_screen.common.values import (cap_to_yuan, end_date,  # noqa: E402
                                        prev_quarter, pub_date)
from kline_screen.common.westock import md  # noqa: E402
from kline_screen.datasources import industry as industry_mod  # noqa: E402
from kline_screen.datasources.kline import (bar_at, bar_high, bar_low,  # noqa: E402
                                            to_bar)
from kline_screen.datasources.market import cross_sig  # noqa: E402
from kline_screen.metrics.health import health_metrics  # noqa: E402
from kline_screen.metrics.statements import (_order_proxy,  # noqa: E402
                                             _stmt_annual, _stmt_latest)
from kline_screen.metrics.turnover import (annotate_turnover,  # noqa: E402
                                           apply_turnover)
from kline_screen.metrics.valuation import valuation_metrics  # noqa: E402
from kline_screen.output import csv_row, turn_cell, turn_star  # noqa: E402
from kline_screen.rules import find_start, judge  # noqa: E402


def make_bars(n=80, breakout=70):
    """前 breakout 根横盘 10 元，之后每天涨 0.1 元；启动日放量 5 倍。"""
    bars = []
    for i in range(n):
        price = 10.0 if i < breakout else 10.0 + (i - breakout + 1) * 0.1
        volume = 100.0
        if i == breakout:
            volume = 500.0
        bars.append((f"D{i:03d}", price, volume, price, price, price, price * volume))
    return bars


def _report(end, pub, **extra):
    row = {"EndDate": end, "InfoPublDate": pub}
    row.update(extra)
    return row


class ScreenHelpersTest(unittest.TestCase):
    def test_md_parses_table_and_skips_separator(self):
        text = """说明
| 代码 | 名称 |
|---|---|
| sh600000 | 浦发银行 |
| sz000001 | 平安银行 |
"""
        rows = md(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["代码"], "sh600000")
        self.assertEqual(rows[1]["名称"], "平安银行")

    def test_md_returns_empty_for_non_table(self):
        self.assertEqual(md("没有表格"), [])

    def test_cross_tpl_requests_flow_fields_without_filtering(self):
        """净流入字段仍须被表达式引用（否则服务端不返回），但不再参与筛选。"""
        self.assertIn("MainNetFlow", p.CROSS_TPL)
        self.assertIn("MainNetFlow5D", p.CROSS_TPL)
        self.assertNotIn("MainNetFlow > 0", p.CROSS_TPL)
        self.assertNotIn("MainNetFlow5D > 0", p.CROSS_TPL)

    def test_bar_at_supports_old_three_tuple_cache(self):
        old = ("2026-10-10", 10.0, 1000.0)
        self.assertEqual(bar_at(old, 0), "2026-10-10")
        self.assertEqual(bar_at(old, 1), 10.0)
        self.assertIsNone(bar_at(old, 4))
        self.assertIsNone(bar_low(old))
        self.assertIsNone(bar_high(old))

    def test_to_bar_order(self):
        bar = to_bar({
            "date": "2026-10-10", "last": 10.5, "volume": 100,
            "open": 10.0, "high": 10.8, "low": 9.9, "amount": 1050,
        })
        self.assertEqual(bar[0], "2026-10-10")
        self.assertEqual(bar[1], 10.5)
        self.assertEqual(bar[2], 100.0)
        self.assertEqual(bar[3], 10.0)
        self.assertEqual(bar[4], 10.8)
        self.assertEqual(bar[5], 9.9)
        self.assertEqual(bar[6], 1050.0)

    def test_cap_to_yuan_normalizes_yi_and_yuan(self):
        self.assertIsNone(cap_to_yuan(None))
        self.assertAlmostEqual(cap_to_yuan(100), 100 * 1e8)
        self.assertAlmostEqual(cap_to_yuan(10_000_000), 10_000_000)

    def test_cross_sig_is_stable_and_param_sensitive(self):
        a = cross_sig(0.5, 20.5)
        b = cross_sig(0.5, 20.5)
        c = cross_sig(0.6, 20.5)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_turn_cell(self):
        self.assertEqual(turn_cell("", "x"), "")
        self.assertEqual(turn_cell("不适用", "x"), "不适用")
        self.assertEqual(turn_cell("是", ""), "是")
        self.assertEqual(turn_cell("是", "毛利率 20%"), "是：毛利率 20%")

    def test_turn_star_buckets(self):
        self.assertEqual(p.CSV_HEADER[2:4], ["一级行业", "二级行业"])
        self.assertEqual(p.CSV_HEADER[15], "分位星级")
        self.assertEqual(len(p.CSV_HEADER), 20)
        self.assertEqual(p.CSV_HEADER[16:], ["最新收盘价", "PE", "PB", "PS"])
        self.assertNotIn("订单增长", p.CSV_HEADER)
        self.assertFalse(any(h.startswith("DCF") for h in p.CSV_HEADER))
        self.assertEqual(turn_star(0), "***")
        self.assertEqual(turn_star(19.9), "***")
        self.assertEqual(turn_star(20), "**")
        self.assertEqual(turn_star(39.9), "**")
        self.assertEqual(turn_star(40), "*")
        self.assertEqual(turn_star(59.9), "*")
        self.assertEqual(turn_star(60), "")
        self.assertEqual(turn_star(100), "")
        self.assertEqual(turn_star(None), "")

    def test_order_proxy(self):
        self.assertEqual(_order_proxy({"ContractLiability": "10", "AdvanceReceipts": "5"}), 15.0)
        self.assertEqual(_order_proxy({"ContractLiability": "10"}), 10.0)
        self.assertEqual(_order_proxy({"ContractLiability": 0}), 0.0)
        self.assertIsNone(_order_proxy({}))

    def test_statement_date_helpers(self):
        self.assertEqual(pub_date("2026-08-15 00:00:00 +0800 CST"), "2026-08-15")
        self.assertEqual(end_date({"EndDate": "2026-06-30 00:00:00"}), "2026-06-30")
        self.assertEqual(prev_quarter("2026-03-31"), "2025-12-31")
        self.assertEqual(prev_quarter("2026-12-31"), "2026-09-30")
        self.assertIsNone(prev_quarter("2026-05-31"))

    def test_stmt_latest_respects_publish_date(self):
        rows = [
            _report("2024-12-31", "2025-03-01", value=1),
            _report("2025-12-31", "2026-03-01", value=2),
            _report("2026-06-30", "2026-08-01", value=3),
        ]
        latest = _stmt_latest(rows, "2026-06-01")
        self.assertEqual(latest["value"], 2)

    def test_stmt_annual_filters_and_orders(self):
        rows = [
            _report("2024-12-31", "2025-03-01", value=1),
            _report("2025-12-31", "2026-03-01", value=2),
            _report("2025-06-30", "2025-08-01", value=3),
            _report("2026-12-31", "2027-03-01", value=4),
        ]
        annual = _stmt_annual(rows, "2026-06-01", n=2)
        self.assertEqual([r["value"] for r in annual], [1, 2])


class FindStartAndJudgeTest(unittest.TestCase):
    def setUp(self):
        self.bars = make_bars()
        self.S = self.bars[-1][0]
        self.det, self.reason = find_start("sh600000", "测试股", self.bars, self.S)
        self.assertTrue(self.det, self.reason)
        self.code = "sh600000"

    def test_find_start_finds_breakout(self):
        self.assertEqual(self.det["d_star"], "D070")
        self.assertEqual(self.det["age"], 9)
        self.assertGreater(self.det["vcross"], 4.0)
        self.assertIsNotNone(self.det["ma20"])
        self.assertIsNotNone(self.det["ma60"])

    def test_find_start_rejects_empty_and_short_history(self):
        self.assertEqual(find_start("x", "x", [], "D000"), (None, "无K线数据"))
        short = make_bars(n=70)
        got, why = find_start("x", "x", short, short[-1][0])
        self.assertIsNone(got)
        self.assertIn("K线历史不足", why)

    def _cross_row(self):
        return {
            "ChangePCT": "5.0",
            "MainNetFlow": "1000000",
            "MainNetFlow5D": "2000000",
            "IsDelisted": "0",
            "Ifsuspend": "0",
        }

    def test_judge_passes_valid_breakout(self):
        d0 = self.det["d_star"]
        ok, why = judge(
            self.det, self.bars, self.S, 10,
            {d0: {self.code: self._cross_row()}},
            {}, {}, 1.75, 0, p.C2_TOL, p.C3_RATIO,
        )
        self.assertTrue(ok, why)

    def test_judge_rejects_age_volume_and_missing_day(self):
        d0 = self.det["d_star"]
        cross = {d0: {self.code: self._cross_row()}}

        bad = dict(self.det, age=11)
        ok, why = judge(bad, self.bars, self.S, 10, cross, {}, {}, 1.75, 0, p.C2_TOL, p.C3_RATIO)
        self.assertFalse(ok)
        self.assertIn("⑦", why)

        bad = dict(self.det, vcross=1.0)
        ok, why = judge(bad, self.bars, self.S, 10, cross, {}, {}, 1.75, 0, p.C2_TOL, p.C3_RATIO)
        self.assertFalse(ok)
        self.assertIn("②", why)

        ok, why = judge(self.det, self.bars, self.S, 10, {}, {}, {}, 1.75, 0, p.C2_TOL, p.C3_RATIO)
        self.assertFalse(ok)
        self.assertIn("不在本次覆盖窗口", why)

    def test_judge_ignores_negative_net_flow(self):
        """v1.3 起净流入只作展示：负值不应影响裁决。"""
        d0 = self.det["d_star"]
        row = dict(self._cross_row(),
                   MainNetFlow="-5000000", MainNetFlow5D="-8000000")
        ok, why = judge(
            self.det, self.bars, self.S, 10,
            {d0: {self.code: row}},
            {}, {}, 1.75, 0, p.C2_TOL, p.C3_RATIO,
        )
        self.assertTrue(ok, why)

    def test_judge_accepts_ma20_5pct_below_ma60(self):
        """v1.4：MA20/MA60 下限由 0.98 放宽到 0.95。"""
        d0 = self.det["d_star"]
        cross = {d0: {self.code: self._cross_row()}}
        det = dict(self.det, m60s=self.det["m20s"] / 0.96)
        ok, why = judge(det, self.bars, self.S, 10, cross, {}, {},
                           p.K_VOL, 0, p.C2_TOL, p.C3_RATIO)
        self.assertTrue(ok, why)
        det = dict(self.det, m60s=self.det["m20s"] / 0.94)
        ok, why = judge(det, self.bars, self.S, 10, cross, {}, {},
                           p.K_VOL, 0, p.C2_TOL, p.C3_RATIO)
        self.assertFalse(ok)
        self.assertIn("④", why)

    def test_judge_uses_3day_slope_window(self):
        """v1.4：MA20 斜率回看由 5 日改为 3 日。

        构造：MA20(d*) 较 5 日前跌 1.85%（旧口径会剔除），
        但较 3 日前高 0.15%（新口径应通过）。
        """
        closes = {66: 8.0, 67: 8.0, 68: 9.8, 69: 9.5, 70: 11.0}
        bars = [(d, closes.get(j, c), v, o, h, l, a)
                for j, (d, c, v, o, h, l, a) in enumerate(make_bars())]
        S = bars[-1][0]
        det, why = find_start(self.code, "测试股", bars, S)
        self.assertTrue(det, why)
        cross = {det["d_star"]: {self.code: self._cross_row()}}
        ok, why = judge(det, bars, S, 10, cross, {}, {},
                           p.K_VOL, 0, p.C2_TOL, p.C3_RATIO)
        self.assertTrue(ok, why)

    def test_judge_rejects_st_on_start_day(self):
        d0 = self.det["d_star"]
        ok, why = judge(
            self.det, self.bars, self.S, 10,
            {d0: {self.code: self._cross_row()}},
            {d0: {self.code}}, {}, 1.75, 0, p.C2_TOL, p.C3_RATIO,
        )
        self.assertFalse(ok)
        self.assertIn("⑤ ST", why)


class TurnoverTest(unittest.TestCase):
    CAL = [f"D{i:03d}" for i in range(20)]
    CAP = 1e9

    def _bars(self, amount):
        return [(d, 10.0, 100.0, 10.0, 10.0, 10.0, amount) for d in self.CAL]

    def _pool(self):
        # 30 只，换手从 0.1% 线性到 3.0%
        return {f"sh60{i:04d}": self._bars(1e6 * (i + 1)) for i in range(30)}

    def _caps(self):
        return {f"sh60{i:04d}": [self.CAP] for i in range(30)}

    def test_annotate_sets_turn_and_rank(self):
        km = self._pool()
        hits = [{"code": c} for c in ("sh600000", "sh600015", "sh600029")]
        pool_t = annotate_turnover(hits, km, km, self._caps(), self.CAL)
        self.assertEqual(len(pool_t), 30)
        self.assertAlmostEqual(hits[0]["turn20"], 0.1, places=3)
        self.assertAlmostEqual(hits[1]["turn20"], 1.6, places=3)
        self.assertAlmostEqual(hits[2]["turn20"], 3.0, places=3)
        self.assertEqual(hits[0]["turn20_rank"], 0.0)
        self.assertGreater(hits[2]["turn20_rank"], 95.0)

    def test_pct_zero_only_annotates(self):
        km = self._pool()
        hits = [{"code": c} for c in ("sh600000", "sh600015", "sh600029")]
        kept, meta, dropped = apply_turnover(
            hits, km, km, self._caps(), self.CAL, 0.0, 5, 30)
        self.assertEqual(len(kept), 3)
        self.assertEqual(dropped, [])
        self.assertFalse(meta["启用"])
        self.assertEqual(meta["模式"], "仅排序/标注")
        self.assertIsNotNone(kept[0]["turn20"])

    def test_pct_04_hard_filters(self):
        km = self._pool()
        hits = [{"code": c} for c in ("sh600000", "sh600015", "sh600029")]
        kept, meta, dropped = apply_turnover(
            hits, km, km, self._caps(), self.CAL, 0.4, 0, 100)
        self.assertTrue(meta["启用"])
        self.assertEqual([x["code"] for x in kept], ["sh600000"])
        self.assertEqual(len(dropped), 2)

    def test_insufficient_data_kept_and_flagged(self):
        km = self._pool()
        km["sh600099"] = self._bars(1e6)[:5]     # 只有 5 天，不足 15 天
        hits = [{"code": "sh600099"}]
        kept, meta, dropped = apply_turnover(
            hits, km, km, self._caps(), self.CAL, 0.4, 0, 100)
        self.assertEqual(len(kept), 1)
        self.assertIsNone(kept[0]["turn20"])
        self.assertIsNone(kept[0]["turn20_rank"])
        self.assertEqual(dropped, [])


class HealthMetricsTest(unittest.TestCase):
    def _normal_rows(self):
        inc = [
            _report("2023-12-31", "2024-03-01", OperatingRevenue=100),
            _report("2024-12-31", "2025-03-01", OperatingRevenue=110),
            _report("2025-12-31", "2026-03-01", OperatingRevenue=121),
        ]
        bal = []
        for end, pub, ar in (
            ("2023-12-31", "2024-03-01", 20.0),
            ("2024-12-31", "2025-03-01", 22.0),
            ("2025-12-31", "2026-03-01", 24.2),
        ):
            bal.append(_report(
                end, pub,
                CashEquivalents=100,
                ShortTermLoan=50,
                TotalCurrentAssets=200,
                Inventories=50,
                TotalCurrentLiability=100,
                BillAccReceivable=ar,
            ))
        ratio = [
            _report("2024-12-31", "2025-03-01", NPParentCompanyOwners=10, NetOperateCashFlow=10),
            _report("2025-12-31", "2026-03-01", NPParentCompanyOwners=10, NetOperateCashFlow=10),
        ]
        return inc, bal, ratio

    def test_normal_metrics(self):
        inc, bal, ratio = self._normal_rows()
        m = health_metrics(inc, bal, ratio, "2026-06-01")
        self.assertEqual(m["level"], "正常", m)
        self.assertAlmostEqual(m["cash_debt"], 2.0)
        self.assertAlmostEqual(m["quick"], 1.5)
        self.assertTrue(m["net_bad"] is False)
        self.assertTrue(m["ar_bad"] is False)

    def test_high_risk_metrics(self):
        inc = [
            _report("2023-12-31", "2024-03-01", OperatingRevenue=100),
            _report("2024-12-31", "2025-03-01", OperatingRevenue=110),
            _report("2025-12-31", "2026-03-01", OperatingRevenue=121),
        ]
        bal = []
        for end, pub, ar in (
            ("2023-12-31", "2024-03-01", 20.0),
            ("2024-12-31", "2025-03-01", 30.0),
            ("2025-12-31", "2026-03-01", 45.0),
        ):
            bal.append(_report(
                end, pub,
                CashEquivalents=10,
                ShortTermLoan=100,
                TotalCurrentAssets=100,
                Inventories=80,
                TotalCurrentLiability=100,
                BillAccReceivable=ar,
            ))
        ratio = [
            _report("2024-12-31", "2025-03-01", NPParentCompanyOwners=10, NetOperateCashFlow=2),
            _report("2025-12-31", "2026-03-01", NPParentCompanyOwners=10, NetOperateCashFlow=2),
        ]
        m = health_metrics(inc, bal, ratio, "2026-06-01")
        self.assertEqual(m["level"], "高危", m)
        self.assertGreaterEqual(m["score"], 2)
        self.assertTrue(m["net_bad"])
        self.assertTrue(m["ar_bad"])

    def test_missing_data(self):
        m = health_metrics([], [], [], "2026-06-01")
        self.assertEqual(m["level"], "数据不足")
        self.assertEqual(m["score"], 0)


class AtomicWriteTest(unittest.TestCase):
    def test_json_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "sub" / "data.json")
            store.jdump(path, {"a": 1, "中文": "值"})
            self.assertEqual(store.jload(path, None), {"a": 1, "中文": "值"})
            self.assertFalse(Path(path + ".tmp").exists())

    def test_csv_atomic_write_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "sub" / "out.csv")
            store.write_csv_atomic(path, ["代码", "名称"], [["sh600000", "浦发银行"], ["sz000001", "平安银行"]])
            self.assertEqual(store.count_csv_rows(path), 2)
            self.assertFalse(Path(path + ".tmp").exists())


class CliSmokeTest(unittest.TestCase):
    def test_scripts_show_help_without_network(self):
        for script in (ROOT / "kline-screan.py",
                       ROOT / "evaluation" / "download_history.py",
                       ROOT / "evaluation" / "evaluate.py"):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    [sys.executable, str(script), "--help"],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout.lower())


class DownloadHistoryHelpersTest(unittest.TestCase):
    def test_rule_constants(self):
        self.assertTrue(hasattr(dh.RULE, "CROSS_TPL"))
        self.assertEqual(dh.CROSS_TPL, p.CROSS_TPL)

    def test_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "x" / "data.json")
            dh.jdump(path, {"x": 1})
            self.assertEqual(dh.jload(path, None), {"x": 1})
            self.assertEqual(dh.jload(str(Path(tmp) / "missing.json"), "default"), "default")

    def test_retry_success_and_failure(self):
        self.assertEqual(dh.retry(lambda: "ok", what="test", tries=1), "ok")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(dh.retry(lambda: None, what="test", tries=1))


class ValuationTest(unittest.TestCase):
    """PS 估值列：纯函数、不联网。"""

    def test_ps_uses_latest_period_only(self):
        rows = [{"EndDate": "2025-06-30", "OperatingRevenueTTM": 1e8},
                {"EndDate": "2026-09-30", "OperatingRevenueTTM": None}]
        d = valuation_metrics(rows, [], {"total_market_cap": 5e9})
        self.assertIsNone(d["ps"])          # 最新期缺 TTM → 留空，不回退旧期

    def test_ps_basic(self):
        rows = [{"EndDate": "2026-09-30", "OperatingRevenueTTM": 1e9}]
        d = valuation_metrics(rows, [], {"total_market_cap": 5e9})
        self.assertEqual(d["ps"], 5.0)

    def test_financial_ps_not_applicable(self):
        rows = [{"EndDate": "2025-12-31", "OperatingRevenueTTM": 1e9,
                 "EnterpriseType": "证券公司"}]
        d = valuation_metrics(rows, [], {"total_market_cap": 5e9})
        self.assertIsNone(d["ps"])

    def test_csv_row(self):
        x = {"code": "sh600000", "name": "浦发银行", "d_star": "2026-11-02",
             "sw1": "银行", "sw2": "股份制银行Ⅱ",
             "age": 2, "vcross": 3.14159, "chg": 5.5, "r20": 1.2, "r60": 3.4,
             "turn_p1": "是", "turn_gm": "毛利率 20%", "turn_p2": "无数据",
             "turn_p3": "否", "health_text": "正常：四项未触发",
             "turn20": 1.5, "turn20_rank": 12.3,
             "price": 7.491, "pe": -25.06, "pb": 2.65, "ps": 4.3}
        row = csv_row(x)
        self.assertEqual(len(row), len(p.CSV_HEADER))
        self.assertEqual(len(row), 20)
        self.assertEqual(row[2:4], ["银行", "股份制银行Ⅱ"])
        self.assertEqual(row[6], 3.14)
        self.assertEqual(row[10], "是：毛利率 20%")
        self.assertEqual(row[11], "否")
        self.assertEqual(row[16:], [7.49, -25.06, 2.65, 4.3])
        # 缺字段时不能崩：行业列记「未分类」，估值列留空
        bare = dict(x)
        for k in ("price", "pe", "pb", "ps", "sw1", "sw2"):
            bare.pop(k)
        self.assertEqual(csv_row(bare)[2:4], ["未分类", "未分类"])
        self.assertEqual(csv_row(bare)[16:], ["", "", "", ""])


class IndustryMapTest(unittest.TestCase):
    """行业归属缓存：只展示，任何失败都不能抛异常。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = industry_mod.INDUSTRY_MAP
        industry_mod.INDUSTRY_MAP = str(Path(self.tmp.name) / "industry_map.json")

    def tearDown(self):
        industry_mod.INDUSTRY_MAP = self.old
        self.tmp.cleanup()

    def _write(self, **kw):
        base = {"schema": industry_mod.INDUSTRY_SCHEMA, "ts": time.time(),
                "sw1": {"sh600000": "银行"},
                "sw2": {"sh600000": "股份制银行Ⅱ"}}
        base.update(kw)
        store.jdump(industry_mod.INDUSTRY_MAP, base)

    def test_fresh_cache_needs_no_request(self):
        self._write()
        calls = []
        m = industry_mod.get_industry_map(builder=lambda: calls.append(1) or {})
        self.assertEqual(m["sw2"]["sh600000"], "股份制银行Ⅱ")
        self.assertEqual(calls, [])

    def test_stale_cache_is_rebuilt_and_saved(self):
        self._write(ts=time.time() - 40 * 86400)
        fresh = {"schema": industry_mod.INDUSTRY_SCHEMA, "ts": time.time(),
                 "sw1": {"sz000001": "银行"},
                 "sw2": {"sz000001": "股份制银行Ⅱ"}}
        m = industry_mod.get_industry_map(builder=lambda: fresh)
        self.assertEqual(m["sw1"], {"sz000001": "银行"})
        self.assertEqual(store.jload(industry_mod.INDUSTRY_MAP, {})["sw1"], {"sz000001": "银行"})

    def test_stale_cache_survives_failed_rebuild(self):
        self._write(ts=time.time() - 40 * 86400)
        m = industry_mod.get_industry_map(builder=lambda: None)
        self.assertEqual(m["sw1"], {"sh600000": "银行"})

    def test_rebuild_exception_falls_back_to_empty_map(self):
        m = industry_mod.get_industry_map(builder=lambda: (_ for _ in ()).throw(RuntimeError("断网")))
        self.assertEqual(m, {"sw1": {}, "sw2": {}})

    def test_schema_mismatch_triggers_rebuild(self):
        self._write(schema=0)
        calls = []
        m = industry_mod.get_industry_map(builder=lambda: calls.append(1) or {
            "schema": industry_mod.INDUSTRY_SCHEMA, "ts": time.time(),
            "sw1": {"sh600000": "银行"}, "sw2": {}})
        self.assertEqual(calls, [1])
        self.assertEqual(m["sw1"], {"sh600000": "银行"})


if __name__ == "__main__":
    unittest.main()
