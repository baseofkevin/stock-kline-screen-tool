"""交易日「是否已收盘定稿」的判断，以及缓存定稿校验。

为什么需要这个模块
------------------
选股的三个核心口径都只在收盘后才成立：

  · 日 K 的**收盘价** —— find_start 的「真突破」判定、MA20/MA60 距离；
  · 当日**成交量** —— 量能倍数 = 启动日量 / 前 5 日均量；
  · 当日**成交额** —— 20 日换手% 的分子。

而服务端在开盘前和盘中**照样会返回一份「当天」的粗筛名单和部分 K 线**。于是：

  1. `get_cross('2026-11-10')` 在开盘前反问回 1527 只「11-10 有成交」的票；
  2. 此时一只股票的 11-10 日 K 都还没生成；
  3. `require_codes` 非空 + K 线全缺 → kline.py 的收尾校验抛 RuntimeError，
     报错还建议「加 --refresh」（重下一遍照样没有当天 K 线，纯属误导）。

更隐蔽的是第 4 步：那份盘中快照会被写进 `cross_<day>.json`，并带上匹配的
缓存签名。等收盘后重跑时它被**当成定稿数据直接复用**，于是筛出来的池子是按
盘中（甚至前一日）的涨幅选的，而且一句提示都没有。

2026-11-10 的真实证据
--------------------
  09:04:33  写入 cross_2026-11-10.json（1527 只，池子已定）
  09:13:39  写入 kline_..._2026-11-10.json（一根 11-10 的 bar 都没有）
  09:30     开盘
  10:10     复查服务端：kline sh600000 --start 2026-11-10 有 bar（last 10.07）
            但缓存里 sz301718 的 ClosePrice=57.48，服务端对同一天返回 56.48

所以两件事都要做：**收盘前直接拒绝运行**（见 pipeline._prepare），以及
**让缓存自己知道是不是收盘前抓的**（见 cache_final）。
"""
import datetime as dt
import os

#: 收盘 15:00 + 5 分钟缓冲，留给数据源结算当日成交与收盘价。
MARKET_CLOSE = dt.time(15, 5)


def _as_date(v):
    return dt.date.fromisoformat(v) if isinstance(v, str) else v


def _naive(t):
    """去掉 tzinfo：与 dt.time 比较时 naive/aware 混用会 TypeError。"""
    return t.replace(tzinfo=None) if t.tzinfo is not None else t


def session_final(day, now=None):
    """该交易日的行情是否已经收盘定稿。

    day 早于今天就一定定稿；等于今天要看是否已过 MARKET_CLOSE；
    晚于今天（不该出现）一律按未定稿处理。
    """
    now = _naive(now or dt.datetime.now())
    d = _as_date(day)
    if d != now.date():
        return d < now.date()
    return now.time() >= MARKET_CLOSE


def cache_final(path, day, meta=None):
    """缓存文件里装的到底是不是「该日收盘定稿」的数据。

    新缓存由写入方在 meta 里记 `final`，直接采信；旧缓存没有该字段，则按
    文件写入时间兜底：写于该日收盘之前的，说明是盘中快照，不能当定稿用。

    兜底规则只在「写入日 == 数据日」时才可能判成快照；数据日早于写入日
    （历史日事后补抓）一律视为定稿。这样既纠正了 2026-11-09、2026-11-10
    这两份盘中缓存，又不会让几百份历史 cross 缓存全部失效。
    """
    if meta and "final" in meta:
        return bool(meta["final"])
    try:
        written = dt.datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return False
    d = _as_date(day)
    if d != written.date():
        return d < written.date()
    return written.time() >= MARKET_CLOSE
