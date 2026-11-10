#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「收盘定稿」判断与缓存定稿校验的测试：不联网、不依赖 database/ 缓存。

这些用例对应 2026-11-10 的真实故障，可直接拿来回归：

  09:04:33  开盘前抓当日粗筛 → cross_2026-11-10.json（1527 只，池子已定）
  09:13:39  抓 K 线 → 一根 11-10 的 bar 都没有
  09:30     开盘
  ~09:14    运行在 kline.py 的收尾校验处抛 RuntimeError（报错还建议 --refresh）

当天 10:10 复查服务端：kline sh600000 --start 2026-11-10 已有 bar，
而缓存里 sz301718 的 ClosePrice=57.48，服务端对同一天返回 56.48 ——
说明那份"11-10 收盘数据"其实是盘中快照，且会被收盘后的重跑直接复用。

运行：
    python3 -m unittest tests.test_session -v
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kline_screen import pipeline  # noqa: E402
from kline_screen.common import session as sess  # noqa: E402
from kline_screen.common.storage import jdump, jload  # noqa: E402
from kline_screen.datasources import kline as kmod  # noqa: E402
from kline_screen.datasources import market  # noqa: E402

DAY = "2026-11-10"


def _touch(path, when):
    """按指定时刻伪造文件写入时间（本地时区）。"""
    ts = when.timestamp()
    os.utime(path, (ts, ts))


class SessionFinalTest(unittest.TestCase):
    """session_final：只有"收盘之后"或"更早的交易日"才算定稿。"""

    def test_past_day_is_final(self):
        self.assertTrue(sess.session_final("2026-11-06",
                                          dt.datetime(2026, 11, 10, 9, 4)))

    def test_today_before_close_is_not_final(self):
        # 09:04 开盘前、11:18 盘中、14:59 收盘前一分钟 —— 都还没定稿
        for hh, mm in ((9, 4), (11, 18), (14, 59), (15, 0)):
            with self.subTest(at=f"{hh:02d}:{mm:02d}"):
                self.assertFalse(sess.session_final(
                    DAY, dt.datetime(2026, 11, 10, hh, mm)))

    def test_today_after_close_is_final(self):
        for hh, mm in ((15, 5), (15, 30), (23, 59)):
            with self.subTest(at=f"{hh:02d}:{mm:02d}"):
                self.assertTrue(sess.session_final(
                    DAY, dt.datetime(2026, 11, 10, hh, mm)))

    def test_future_day_is_not_final(self):
        self.assertFalse(sess.session_final("2026-11-11",
                                           dt.datetime(2026, 11, 10, 21, 0)))

    def test_aware_datetime_does_not_raise(self):
        aware = dt.datetime(2026, 11, 10, 10, 0, tzinfo=dt.timezone.utc)
        self.assertFalse(sess.session_final(DAY, aware))

    def test_accepts_date_object(self):
        self.assertTrue(sess.session_final(dt.date(2026, 11, 6),
                                          dt.datetime(2026, 11, 10, 9, 4)))


class CacheFinalTest(unittest.TestCase):
    """cache_final：meta 优先，没有 meta 时按写入时间兜底。"""

    def test_meta_final_wins_over_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cross.json")
            jdump(path, [])
            # 文件是刚刚写的（今天），但 meta 明确记为定稿
            _touch(path, dt.datetime(2026, 11, 6, 20, 0))
            self.assertTrue(sess.cache_final(path, "2026-11-06", {"final": True}))
            self.assertFalse(sess.cache_final(path, "2026-11-06", {"final": False}))

    def test_same_day_write_before_close_is_not_final(self):
        """09:04 写出的"当日"缓存 = 盘中快照。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cross.json")
            jdump(path, [])
            _touch(path, dt.datetime(2026, 11, 10, 9, 4))
            self.assertFalse(sess.cache_final(path, DAY, {}))

    def test_same_day_write_after_close_is_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cross.json")
            jdump(path, [])
            _touch(path, dt.datetime(2026, 11, 10, 15, 30))
            self.assertTrue(sess.cache_final(path, DAY, {}))

    def test_older_day_cache_written_later_is_final(self):
        """历史日事后补抓（写入日晚于数据日）一律算定稿，否则几百份缓存全失效。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cross.json")
            jdump(path, [])
            _touch(path, dt.datetime(2026, 11, 10, 9, 4))
            self.assertTrue(sess.cache_final(path, "2026-11-06", {}))

    def test_missing_file_is_not_final(self):
        self.assertFalse(sess.cache_final("/nonexistent/x.json", DAY, {}))


class PrepareGuardTest(unittest.TestCase):
    """pipeline._prepare：收盘前直接拒绝，且不给任何网络请求留机会。"""

    DAYS = ["2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04",
            "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]

    def _args(self, **kw):
        d = dict(date=None, n=4, k_vol=1.5, chg_lo=0.5, chg_hi=20.5,
                 turn_pct=0.0, turn_min=5, turn_max=30, ma60_gap=0.0,
                 refresh=False, refresh_industry=False)
        d.update(kw)
        return argparse.Namespace(**d)

    def _prepare(self, args, final, tmp):
        with mock.patch.object(pipeline, "DB", tmp), \
                mock.patch.object(pipeline, "CSVDIR", tmp), \
                mock.patch.object(pipeline, "get_trading_days",
                                  return_value=list(self.DAYS)), \
                mock.patch.object(pipeline, "session_final", return_value=final), \
                contextlib.redirect_stdout(io.StringIO()):
            st = {"a": args, "DB": tmp}
            pipeline._prepare(st)
            return st

    def test_rejects_unfinalised_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(pipeline.CliError) as ctx:
                self._prepare(self._args(), False, tmp)
        msg = str(ctx.exception)
        self.assertIn("尚未收盘定稿", msg)
        self.assertIn("15:05", msg)
        # 必须直接告诉用户该用哪一天，否则不知道下一步怎么办
        self.assertIn("--date 2026-11-09", msg)

    def test_rejects_explicit_today_before_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(pipeline.CliError) as ctx:
                self._prepare(self._args(date=DAY), False, tmp)
        self.assertIn(DAY, str(ctx.exception))

    def test_accepts_finalised_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._prepare(self._args(date="2026-11-06"), True, tmp)
        self.assertEqual(st["S"], "2026-11-06")
        self.assertEqual(st["need"], ["2026-11-02", "2026-11-03",
                                      "2026-11-04", "2026-11-05", "2026-11-06"])

    def test_default_baseline_is_latest_trading_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = self._prepare(self._args(), True, tmp)
        self.assertEqual(st["S"], DAY)


class CrossCacheFinalityTest(unittest.TestCase):
    """get_cross：读到非定稿缓存必须重拉，读到定稿缓存不得联网。"""

    ROWS = [{"code": "sh600000", "Closing": "10.0", "ChangePCT": "1.0",
             "MainNetFlow": "1", "MainNetFlow5D": "1"}]

    def _seed(self, tmp, day, written, with_final=None):
        path = os.path.join(tmp, f"cross_{day}.json")
        meta_path = os.path.join(tmp, f"cross_{day}.meta.json")
        jdump(path, [{"code": "stale"}])
        meta = {"sig": market.cross_sig(0.5, 20.5), "version": 3}
        if with_final is not None:
            meta["final"] = with_final
        jdump(meta_path, meta)
        _touch(path, written)
        _touch(meta_path, written)
        return path

    def _call(self, tmp, day):
        calls = []

        def fake_retry(fn, **kw):
            calls.append(1)
            return list(self.ROWS)

        with mock.patch.object(market, "DB", tmp), \
                mock.patch.object(market, "retry", side_effect=fake_retry), \
                mock.patch.object(market.time, "sleep", return_value=None):
            rows = market.get_cross(day, 0.5, 20.5, False)
        return rows, len(calls)

    def test_intraday_cache_is_refetched(self):
        """2026-11-10 现场：meta 无 final、写于 09:04 → 必须重拉。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._seed(tmp, DAY, dt.datetime(2026, 11, 10, 9, 4))
            rows, n = self._call(tmp, DAY)
            self.assertEqual(n, 1, "收盘前抓的缓存必须重拉，不能复用")
            self.assertEqual(rows, self.ROWS)
            self.assertIn("final", jload(path[:-5] + ".meta.json", {}),
                          "重拉后必须把定稿状态写进 meta")

    def test_meta_final_false_beats_recent_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, "2026-11-06", dt.datetime(2026, 11, 6, 20, 0),
                       with_final=False)
            _, n = self._call(tmp, "2026-11-06")
            self.assertEqual(n, 1)

    def test_finalised_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp, "2026-11-06", dt.datetime(2026, 11, 8, 11, 30))
            rows, n = self._call(tmp, "2026-11-06")
            self.assertEqual(n, 0, "定稿缓存不应触发网络请求")
            self.assertEqual(rows, [{"code": "stale"}])


class KlineTailTest(unittest.TestCase):
    """_drop_unfinal_tail：写于收盘前的底稿，丢掉当日那根半成品 bar。"""

    def _km(self):
        return {"sh600000": [["2026-11-06", 10.0], [DAY, 10.4]],
                "sz000001": [["2026-11-06", 11.0], [DAY, 11.2]],
                "sh601390": []}

    def _path(self, tmp, written, **meta):
        path = os.path.join(tmp, f"kline_2026-01-08_{DAY}.json")
        jdump(path, [])
        if meta:
            jdump(kmod._kline_meta(path), meta)
        _touch(path, written)
        return path

    def test_intraday_base_loses_today_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._path(tmp, dt.datetime(2026, 11, 10, 9, 13))
            km = self._km()
            with contextlib.redirect_stderr(io.StringIO()) as err:
                out = kmod._drop_unfinal_tail(km, path, DAY)
            self.assertEqual([b[0] for b in out["sh600000"]], ["2026-11-06"])
            self.assertEqual([b[0] for b in out["sz000001"]], ["2026-11-06"])
            self.assertIn("半成品", err.getvalue())

    def test_finalised_base_keeps_today_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._path(tmp, dt.datetime(2026, 11, 10, 15, 30),
                              end=DAY, final=True)
            km = self._km()
            out = kmod._drop_unfinal_tail(km, path, DAY)
            self.assertEqual(len(out["sh600000"]), 2)

    def test_stale_old_day_is_untouched(self):
        """end 早于写入日（历史日事后补抓）不丢任何 bar。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._path(tmp, dt.datetime(2026, 11, 10, 15, 30))
            km = self._km()
            out = kmod._drop_unfinal_tail(km, path, "2026-11-06")
            self.assertEqual(len(out["sh600000"]), 2)

    def test_empty_map_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._path(tmp, dt.datetime(2026, 11, 10, 9, 13))
            self.assertEqual(kmod._drop_unfinal_tail({}, path, DAY), {})


def _weekday_bars(n, last="2026-11-06"):
    """从 last 往回取 n 个交易日，构造 3 元组 bar（date, close, volume）。"""
    d = dt.date.fromisoformat(last)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append([d.isoformat(), 10.0, 100.0])
        d -= dt.timedelta(days=1)
    return out[::-1]


class KlineStaleRoundsTest(unittest.TestCase):
    """get_kline 的收尾校验：基准日有成交却缺 K 线时，按进展重试多轮。"""

    END = "2026-11-09"

    def _run(self, tmp, fake_batch, base_bars, require=("sh600000",)):
        with mock.patch.object(kmod, "DB", tmp), \
                mock.patch.object(kmod, "_load_kline_base",
                                  return_value={"sh600000": base_bars}), \
                mock.patch.object(kmod, "_kline_batch", side_effect=fake_batch), \
                mock.patch.object(kmod.time, "sleep", return_value=None), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            km, missing = kmod.get_kline(
                ["sh600000"], "2026-08-01", self.END, False,
                require_codes=set(require))
        return km, missing, err.getvalue()

    def test_recovers_in_later_round_when_throttled(self):
        """第 1 次（增量补新）被限频打空，后面的收尾轮次把 K 线补齐。"""
        full = _weekday_bars(80) + [[self.END, 10.5, 200.0]]
        rows = [{"code": "sh600000", "date": b[0], "last": str(b[1]),
                 "volume": str(b[2])} for b in full]
        calls = []

        def fake(ch, beg, end, tries=3, allow_empty=False):
            calls.append(beg)
            return None if len(calls) == 1 else list(rows)

        with tempfile.TemporaryDirectory() as tmp:
            km, missing, err = self._run(tmp, fake, _weekday_bars(80))
        self.assertEqual(km["sh600000"][-1][0], self.END)
        self.assertEqual(missing, [])
        self.assertIn("轮补取", err)
        self.assertIn("本轮补上 1 只", err)

    def test_retries_stale_codes_outside_require_codes(self):
        """不在当日粗筛池里的陈旧票也必须补，否则会被 ③ 当停牌静默剔除。"""
        full = _weekday_bars(80) + [[self.END, 10.5, 200.0]]
        rows = [{"code": "sh600000", "date": b[0], "last": str(b[1]),
                 "volume": str(b[2])} for b in full]
        calls = []

        def fake(ch, beg, end, tries=3, allow_empty=False):
            calls.append(beg)
            return None if len(calls) == 1 else list(rows)

        with tempfile.TemporaryDirectory() as tmp:
            km, _, err = self._run(tmp, fake, _weekday_bars(80), require=())
        self.assertEqual(km["sh600000"][-1][0], self.END)
        self.assertGreater(len(calls), 1, "非 pool 内的陈旧票也应触发补取")
        self.assertIn("本轮补上 1 只", err)

    def test_raises_only_when_no_progress(self):
        """每轮都毫无进展 → 必须报错，并给出可执行的下一步。"""
        def fake(ch, beg, end, tries=3, allow_empty=False):
            return None

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                self._run(tmp, fake, _weekday_bars(80))
        msg = str(ctx.exception)
        self.assertIn("K线未更新且重取失败", msg)
        self.assertIn("轮内补取", msg)
        self.assertIn("隔几分钟重跑", msg)

    def test_no_raise_when_base_already_fresh(self):
        """底稿末日已到 end → 一个请求都不该发。"""
        calls = []

        def fake(ch, beg, end, tries=3, allow_empty=False):
            calls.append(beg)
            return []

        with tempfile.TemporaryDirectory() as tmp:
            km, missing, _ = self._run(
                tmp, fake, _weekday_bars(80) + [[self.END, 10.2, 120.0]])
        self.assertEqual(len(calls), 0)
        self.assertEqual(km["sh600000"][-1][0], self.END)


class KlineSplitTest(unittest.TestCase):
    """_kline_fetch：一批只回一部分时，当场拆半把缺的补齐（不靠等待）。"""

    CODES = [f"sh60{i:04d}" for i in range(60)]

    def test_splits_until_server_cap_is_met(self):
        calls = []

        def fake(ch, beg, end, tries=3, allow_empty=False):
            calls.append(len(ch))
            return [{"code": c, "date": "2026-11-09", "last": "10", "volume": "100"}
                    for c in ch[:10]]          # 服务端每次只回 10 只

        with mock.patch.object(kmod, "_kline_batch", side_effect=fake):
            got = kmod._kline_fetch(self.CODES, "2026-11-07", "2026-11-09",
                                    allow_empty=True)
        self.assertEqual(set(got), set(self.CODES))
        self.assertGreater(len(calls), 1, "丢码时必须拆批重取")
        self.assertLessEqual(min(calls), kmod.KLINE_SPLIT_MIN,
                             "应当拆到最小粒度，否则服务端仍会截断")
        self.assertLessEqual(len(calls), 2 ** (kmod.KLINE_SPLIT_DEPTH + 2),
                             "拆分层数必须有上限，避免请求爆炸")

    def test_no_split_when_batch_complete(self):
        calls = []

        def fake(ch, beg, end, tries=3, allow_empty=False):
            calls.append(len(ch))
            return [{"code": c, "date": "2026-11-09", "last": "10", "volume": "1"}
                    for c in ch]

        with mock.patch.object(kmod, "_kline_batch", side_effect=fake):
            got = kmod._kline_fetch(self.CODES, "2026-11-07", "2026-11-09")
        self.assertEqual(len(got), len(self.CODES))
        self.assertEqual(len(calls), 1, "完整返回不应触发拆批")

    def test_returns_none_when_batch_fails(self):
        with mock.patch.object(kmod, "_kline_batch", return_value=None):
            self.assertIsNone(kmod._kline_fetch(["sh600000"], "a", "b"))


class KlineCoverageGateTest(unittest.TestCase):
    """③ 兜底：仍有陈旧票时，用当日行情区分「限频漏取」与「真停牌」。"""

    END = "2026-11-09"

    def _run(self, tmp, verdict):
        def no_bar(ch, beg, end, tries=3, allow_empty=False):
            return None

        with mock.patch.object(kmod, "DB", tmp), \
                mock.patch.object(kmod, "_load_kline_base",
                                  return_value={"sh600000": _weekday_bars(80)}), \
                mock.patch.object(kmod, "_kline_batch", side_effect=no_bar), \
                mock.patch.object(kmod, "_traded_on", return_value=dict(verdict)), \
                mock.patch.object(kmod.time, "sleep", return_value=None), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            km, missing = kmod.get_kline(["sh600000"], "2026-08-01", self.END,
                                         False, require_codes=set())
        return km, missing, err.getvalue()

    def test_traded_but_missing_raises(self):
        """当日有成交却拿不到 K 线 → 绝不能当停牌剔除，必须报错。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                self._run(tmp, {"sh600000": True})
        msg = str(ctx.exception)
        self.assertIn("K线漏取（非停牌）", msg)
        self.assertIn("sh600000", msg)

    def test_suspended_is_only_warned(self):
        """当日无成交 → 真停牌，告警后按「无当日数据」剔除，不报错。"""
        with tempfile.TemporaryDirectory() as tmp:
            km, missing, err = self._run(tmp, {"sh600000": False})
        self.assertEqual(len(km), 1)
        self.assertIn("当日无成交（停牌）", err)

    def test_unverifiable_is_flagged_not_passed_silently(self):
        """行情核对本身失败 → 不能当成「没成交」，必须点名说结论不完整。"""
        with tempfile.TemporaryDirectory() as tmp:
            km, missing, err = self._run(tmp, {})
        self.assertEqual(len(km), 1)
        self.assertIn("没能从行情接口核对", err)
        self.assertIn("结论不完整", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
