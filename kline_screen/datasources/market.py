"""交易日历、横截面、标签、行情快照与市值。"""
import datetime as dt
import hashlib
import os
import time

from ..config import DB, W
from ..common.session import cache_final, session_final
from ..common.storage import jdump, jload
from ..common.westock import md, retry, run
from ..params import (CROSS_CACHE_VERSION, CROSS_TPL, LABEL_PAGE, NEW_LABELS)

def cross_sig(lo, hi):
    """横截面缓存签名：表达式 + 参数 + 版本。参数变化 → 缓存自动失效。"""
    expr = CROSS_TPL.format(lo=lo, hi=hi)
    return hashlib.sha1(f"v{CROSS_CACHE_VERSION}|{expr}".encode("utf-8")).hexdigest()


CAL_LIMIT = 2000            # 交易日历单次返回上限（防御性，超过即报错而非静默截断）
CAL_SPAN_DAYS = 365 * 4     # 默认回看 4 年（≈970 个交易日），覆盖回测区间


def get_trading_days(end, refresh, span=None):
    """交易日列表（单文件缓存）。

    起点按 CAL_SPAN_DAYS 动态回看 4 年。若返回条数触到 CAL_LIMIT 则直接报错，
    避免结果被截断后 days[-1] 停在过期日期，main() 却继续用过期基准日跑出
    一份看似正常的名单。
    """
    e = dt.date.fromisoformat(end)
    auto = (e - dt.timedelta(days=CAL_SPAN_DAYS)).isoformat()
    span = auto if span is None else min(span, auto)
    path = os.path.join(DB, "trading_calendar.json")
    cached = jload(path, {})
    if (not refresh and cached.get("end") == end and cached.get("days")
            and cached.get("span") == span):
        return [d for d in cached["days"] if d <= end]
    rows = retry(lambda: md(run([W, "trade-calendar", "--start", span, "--end", end,
                                 "--trading-only", "--limit", str(CAL_LIMIT)])),
                 what="交易日历")
    if rows is None:
        raise RuntimeError("交易日历获取失败，终止")
    if len(rows) >= CAL_LIMIT:
        raise RuntimeError(
            f"交易日历返回 {len(rows)} 条，已达上限 {CAL_LIMIT}，结果可能被截断；"
            f"请调大 CAL_LIMIT 或缩短区间（{span} ~ {end}）")
    ds = sorted(r["date"] for r in rows if r.get("date") and r["date"] <= end)
    if not ds:
        raise RuntimeError(f"交易日历为空（{span} ~ {end}）")
    jdump(path, {"end": end, "span": span, "days": ds})
    return ds


def get_cross(day, lo, hi, refresh):
    """横截面粗筛，逐日单独落盘。

    缓存带签名（表达式 + lo/hi + 版本）。改 --chg-lo/--chg-hi 或 CROSS_TPL 后不会
    复用旧结果；无 meta 的旧缓存一律按新签名重拉。
    空结果仍视为失败：历史日期接口会静默返空，不能把失败当成「当天 0 只」。

    另加一道**定稿校验**（2026-11-10）：收盘前抓到的「当天」粗筛实际是盘中
    快照（服务端照样返回名单，只是涨幅/价格都没定），一旦缓存下来，收盘后
    重跑会直接复用，静默产出按错误涨幅筛出来的池子。所以 meta 里记 final，
    读到非定稿的缓存就重拉。
    """
    path = os.path.join(DB, f"cross_{day}.json")
    meta_path = os.path.join(DB, f"cross_{day}.meta.json")
    sig = cross_sig(lo, hi)
    expr = CROSS_TPL.format(lo=lo, hi=hi)
    if os.path.exists(path) and not refresh:
        meta = jload(meta_path, {})
        if meta.get("sig") != sig:
            # 参数或表达式变化后旧缓存不能复用，避免把旧区间的池子当成新池。
            print(f"      [缓存失效] {day}：参数/表达式已变，重新拉取", flush=True)
        elif not cache_final(path, day, meta):
            print(f"      [缓存失效] {day}：缓存在收盘前抓取（盘中快照），"
                  f"重新拉取", flush=True)
        else:
            return jload(path, [])
    rows = retry(lambda: md(run([W, "screen", "condition", "--expression",
                                 expr, "--date", day, "--limit", "6000"])),
                 what=f"横截面粗筛 {day}")
    if rows is None:
        raise RuntimeError(f"横截面粗筛失败：{day}（不得据此产出名单）")
    jdump(path, rows)
    jdump(meta_path, {"sig": sig, "version": CROSS_CACHE_VERSION,
                      "expression": expr, "final": session_final(day)})
    time.sleep(0.8)
    return rows


def get_labels(day, refresh):
    """某交易日的排除集（ST / 次新股）。

    这里**不做**定稿校验：ST 与上市日期都是「日级」属性，盘中和盘后一致，
    不像收盘价/成交量那样盘中会变。少一次重拉也少一分被限频的风险。
    """
    path = os.path.join(DB, f"labels_{day}.json")
    if os.path.exists(path) and not refresh:
        return jload(path, {"st": [], "new": []})
    st, new = set(), set()
    for L in ["risk_st"] + NEW_LABELS:
        off = 0
        while True:
            rows = retry(lambda: md(run([W, "screen", "label", "--type", L,
                                         "--date", day, "--limit", str(LABEL_PAGE),
                                         "--offset", str(off)])),
                         what=f"标签 {L} @ {day}",
                         # 第 2 页起，空页是正常的「没有更多」；
                         # 首屏为空仍判失败——不能把接口故障当成「没有次新股」。
                         allow_empty=(off > 0))
            if rows is None:
                # 静默继续会导致次新股漏剔，必须让整次运行失败
                raise RuntimeError(f"排除集拉取失败：{L} @ {day}")
            (st if L == "risk_st" else new).update(
                r["code"] for r in rows if r.get("code"))
            if len(rows) < LABEL_PAGE:
                break
            off += LABEL_PAGE
        time.sleep(0.4)
    out = {"st": sorted(st), "new": sorted(new)}
    jdump(path, out)
    return out


def get_quotes(codes, day, refresh):
    """某交易日的行情快照（收盘价等展示列）。

    收盘前抓到的快照价格等于盘中价，与「基准日收盘」口径不符，因此同样做
    定稿校验：缓存非定稿就整份丢弃、重拉本次需要的代码。
    """
    path = os.path.join(DB, f"quote_{day}.json")
    if os.path.exists(path) and not refresh:
        cached = jload(path, {})
        if cached and not cache_final(path, day):
            print(f"      [缓存失效] {day}：行情缓存在收盘前抓取，重新拉取",
                  flush=True)
            cached = {}
        if all(c in cached for c in codes):
            return cached
        out = dict(cached)
    else:
        out = {}
    for i in range(0, len(codes), 20):
        ch = [c for c in codes[i:i + 20] if c not in out]
        if not ch:
            continue
        # 必须传 --date，否则取到实时值，与基准日口径不一致
        rows = retry(lambda: md(run([W, "quote", ",".join(ch), "--date", day])),
                     tries=2, what="行情")
        for r in (rows or []):
            c = r.get("code") or r.get("symbol") or (ch[0] if len(ch) == 1 else None)
            if c:
                out[c] = r
        jdump(path, out)
        time.sleep(0.5)
    return out


def get_mv(day, refresh):
    """某交易日的全市场流通/总市值（**元**），格式 {code: [流通, 总]}。

    市值比的分母必须与分子（d* 日 MainNetFlow）同一天，避免 age=4 时分母
    滞后 4 个交易日，让展示的市值比被一周市值波动明显改变。
    与 download_history.download_mv() 使用同一文件名与同一格式，两个程序共用缓存。

    分母是换手%的归一化基准，收盘前抓的市值与收盘市值不同，因此同样做定稿校验。
    """
    path = os.path.join(DB, f"mv_{day}.json")
    if os.path.exists(path) and not refresh:
        cached = jload(path, {})
        if cached and not cache_final(path, day):
            print(f"      [缓存失效] {day}：市值缓存在收盘前抓取，重新拉取",
                  flush=True)
        elif cached:
            return cached
    rows = retry(lambda: md(run([W, "screen", "condition", "--expression",
                                 "intersect([NegotiableMV > 0, TotalMV > 0])",
                                 "--date", day, "--limit", "8000"])),
                 what=f"市值快照 {day}")
    if rows is None:
        raise RuntimeError(
            f"市值快照失败：{day}。市值比分母无法计算，"
            f"终止以免产出不可信名单（可重跑或加 --refresh）")
    out = {}
    for r in rows:
        c = r.get("code")
        try:
            out[c] = [float(r["NegotiableMV"]), float(r["TotalMV"])]
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        raise RuntimeError(f"市值快照为空：{day}（接口未返回 NegotiableMV/TotalMV）")
    jdump(path, out)
    time.sleep(0.5)
    return out
