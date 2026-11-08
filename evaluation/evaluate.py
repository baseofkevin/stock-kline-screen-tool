#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evaluate.py —— 选股名单的前向评估（只读 database/，不联网）

作用：
    回答「这份名单后来到底涨没涨」。逐期读取 database/result_*.json，
    用 database/kline_*.json 的日线算 5/10/20 个交易日后的收益，并与
    ① 等权全市场、② 同规模桶（流通市值五分位）、③ 当期粗筛池 三个基准比较。
    同时维护 picks_log.csv：新增名单自动落一行，收益列随数据到位自动补齐。

用法：
    python3 evaluation/evaluate.py                 # 评估全部 result_*.json，更新 picks_log.csv
    python3 evaluation/evaluate.py --no-log        # 只看报表，不写 picks_log.csv
    python3 evaluation/evaluate.py --log 自定义.csv
    python3 evaluation/evaluate.py --json eval.json

口径：
    · 收益 = 收盘价(S+h) / 收盘价(S) − 1，S 为名单元数据里的「基准日」；
    · 要求 S 与 S+h 当天都有成交价；S+h 停牌时记「数据不足」，
      不用之前最近一根顶替；
    · 超额 = 个股收益 − 基准收益；基准按「当天有数据」的股票等权平均；
    · 不含交易成本、不含滑点；样本重叠，t 值仅作参考（不输出显著性结论）。
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import glob
import json
import os
import statistics as st
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # database/ 仍在项目根目录
DB = os.path.join(ROOT, "database")
LOG = os.path.join(HERE, "picks_log.csv")   # 与 evaluate.py 同目录
HORIZONS = (5, 10, 20)
MIN_COVER = 500          # 基准最少需要多少只股票同时有 S 与 S+h 的成交价

# picks_log.csv 中由本程序维护的列（顺序即写入顺序；人工结论/理由保留用户填写）
MANAGED = ["日期", "代码", "名称", "启动日", "距启动",
           "20日换手%", "池内分位", "启动日涨停",
           "持有5日%", "持有10日%", "持有20日%",
           "超额5日%", "超额10日%", "超额20日%",
           "人工结论", "理由"]


# ---------------- 纯函数（可单测） ----------------

def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def horizon_ret(dates, closes, S, target):
    """S 收盘买入、target 收盘卖出的收益。

    要求 S 与 target 都有成交价；停牌（target 没有成交）返回 None，
    不用「之前最近一根」顶替——那会把 3 天的涨幅标成 10 日收益。
    """
    if not dates:
        return None
    i = bisect.bisect_right(dates, S) - 1
    if i < 0 or dates[i] != S:
        return None
    j = bisect.bisect_right(dates, target) - 1
    if j <= i or dates[j] != target:
        return None
    return closes[j] / closes[i] - 1


def limit_pct(code):
    """涨跌停幅度：主板 10%、创业板/科创板 20%、北交所 30%。"""
    c = str(code)
    if c.startswith("bj"):
        return 30.0
    if c.startswith("sh688") or c.startswith("sz300") or c.startswith("sz301"):
        return 20.0
    return 10.0


def limit_state(chg_pct, code):
    """启动日涨幅 → 是 / 接近 / 否（留 0.3pct 容差应对四舍五入）。"""
    v = fnum(chg_pct)
    if v is None:
        return ""
    lim = limit_pct(code)
    if v >= lim - 0.3:
        return "是"
    if v >= lim - 2.5:
        return "接近"
    return "否"


def size_bucket(cap, cuts):
    """流通市值 → 0~4 桶；cap 为空或 cuts 为空时返回 None。"""
    if cap is None or not cuts:
        return None
    return sum(1 for c in cuts if cap > c)


def bucket_cuts(caps, qs=(0.2, 0.4, 0.6, 0.8)):
    vals = sorted(v for v in caps if v)
    if len(vals) < 50:
        return []
    return [vals[min(len(vals) - 1, int(len(vals) * q))] for q in qs]


# ---------------- 数据加载 ----------------

def load_bars(db):
    """合并 database/kline_*.json → {code: (dates, closes)}，日期升序。"""
    store = {}
    files = sorted(glob.glob(os.path.join(db, "kline_*.json")))
    if not files:
        raise SystemExit(f"错误：{db} 下没有 kline_*.json，先跑 kline-screan.py 或 evaluation/download_history.py")
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:  # 坏文件不阻断整体评估
            print(f"      ⚠️ 跳过无法读取的 {os.path.basename(path)}：{e}", file=sys.stderr)
            continue
        for code, bars in data.items():
            if not isinstance(bars, list):
                continue
            m = store.setdefault(code, {})
            for b in bars:
                try:
                    m[b[0]] = float(b[1])
                except (TypeError, ValueError, IndexError):
                    continue
    out = {}
    for code, m in store.items():
        ds = sorted(m)
        out[code] = (ds, [m[d] for d in ds])
    if not out:
        raise SystemExit("错误：kline 缓存为空")
    return out


def load_caps_by_day(db):
    """{日期: {code: 流通市值(元)}}，来自 mv_<d>.json。"""
    out = {}
    for path in sorted(glob.glob(os.path.join(db, "mv_*.json"))):
        day = os.path.basename(path)[3:-5]
        try:
            with open(path, encoding="utf-8") as fh:
                mv = json.load(fh)
        except Exception:
            continue
        out[day] = {c: fnum(v[0]) for c, v in mv.items() if isinstance(v, list) and v}
    return out


def caps_on(caps_by_day, S):
    """S 日（或之前最近一天）的流通市值快照。"""
    for d in sorted(caps_by_day, reverse=True):
        if d <= S:
            return caps_by_day[d]
    return {}


def load_pool(db, S):
    path = os.path.join(db, f"cross_{S}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
    except Exception:
        return None
    return {r.get("code") for r in rows if r.get("code")}


def load_results(db):
    out = []
    for path in sorted(glob.glob(os.path.join(db, "result_*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                r = json.load(fh)
        except Exception as e:
            print(f"      ⚠️ 跳过无法读取的 {os.path.basename(path)}：{e}", file=sys.stderr)
            continue
        S = r.get("基准日")
        names = r.get("名单") or []
        if not S or not isinstance(names, list) or not names:
            continue
        out.append({"S": S, "version": r.get("版本", ""), "names": names,
                     "file": os.path.basename(path)})
    return out


def fetch_universe(db, bars, results, end=None):
    """用 download_history 的下载器补齐「全市场日线」，覆盖评估所需的窗口。

    只补 [最早基准日, end]，不重下整段历史；已存在的同名文件会复用。
    """
    import download_history as dh
    end = end or dt.date.today().isoformat()
    # 必须落在真实交易日上：download_kline 的截断校验要求末日 == end，
    # 传周末/节假日会让每只股票都被判定为「截断」而逐只重取。
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=40)).isoformat()
    days = dh.download_calendar(start, end, False)
    if days:
        end = days[-1]
    beg = min(r["S"] for r in results)
    codes = sorted(bars)
    print(f"补全市场日线：{len(codes)} 只 × {beg} ~ {end}（可中断，重跑续传）...")
    print("      实测每批 2–70 秒不等，全市场约 20–60 分钟；建议 nohup 放后台")
    km, missing = dh.download_kline(codes, beg, end, False)
    print(f"      完成 {len(km)} 只，缺 {len(missing)} 只（多为停牌或退市）")


# ---------------- 评估 ----------------

def market_returns(bars, caps, cuts, S, target):
    """返回 (全市场均值, {桶: 均值}, 有效只数)。"""
    tot, buckets = [], defaultdict(list)
    for code, (ds, cl) in bars.items():
        r = horizon_ret(ds, cl, S, target)
        if r is None:
            continue
        tot.append(r)
        b = size_bucket(caps.get(code), cuts)
        if b is not None:
            buckets[b].append(r)
    mean = sum(tot) / len(tot) if tot else None
    bm = {b: sum(v) / len(v) for b, v in buckets.items() if len(v) >= 30}
    return mean, bm, len(tot)


def eval_period(bars, cal, caps_by_day, db, res, windows=HORIZONS):
    S = res["S"]
    caps = caps_on(caps_by_day, S)
    cuts = bucket_cuts(caps.values())
    pool = load_pool(db, S)
    i0 = bisect.bisect_right(cal, S) - 1
    # 基准只需每期每窗口算一次（不要放进个股循环里，否则 O(名单×全市场)）
    base, cover = {}, {}
    for h in windows:
        if i0 + h >= len(cal):
            base[h] = None
            continue
        target = cal[i0 + h]
        mk, bm, ncover = market_returns(bars, caps, cuts, S, target)
        pr = None
        if pool:
            prs = [horizon_ret(*bars[c], S, target) for c in pool if c in bars]
            prs = [v for v in prs if v is not None]
            pr = (sum(prs) / len(prs)) if len(prs) >= 20 else None
        cover[h] = ncover
        base[h] = None if ncover < MIN_COVER else (mk, bm, pr, ncover)
    rows = []
    for x in res["names"]:
        code = x.get("code")
        if not code or code not in bars:
            continue
        ds, cl = bars[code]
        row = {"code": code, "name": x.get("name") or "", "d_star": x.get("d_star") or "",
               "age": x.get("age"), "chg": x.get("chg"),
               "turn20": x.get("turn20"), "turn20_rank": x.get("turn20_rank"),
               "limit": limit_state(x.get("chg"), code)}
        for h in windows:
            b = base.get(h)
            if b is None:
                row[f"r{h}"] = row[f"ex{h}"] = row[f"pool{h}"] = None
                continue
            mk, bm, pr, _ = b
            r = horizon_ret(ds, cl, S, cal[i0 + h])
            row[f"r{h}"] = r
            # 同规模桶（无 cap 时退回全市场）
            bk = size_bucket(caps.get(code), cuts)
            ref = bm.get(bk) if (bk is not None and bk in bm) else mk
            row[f"ex{h}"] = (r - ref) if (r is not None and ref is not None) else None
            row[f"mkt{h}"] = mk
            row[f"pool{h}"] = pr
        rows.append(row)
    return rows, cover


def mean_of(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def fmt_pct(v, width=8, sign=True):
    if v is None:
        return " " * (width - 1) + "-"
    return f"{v*100:+{width-1}.2f}%"


def print_report(periods, agg):
    print("\n" + "=" * 78)
    print("前向评估：名单的 5/10/20 日收益与超额（基准=同规模桶；括号内为等权全市场）")
    print("=" * 78)
    print(f"{'期间':12s}{'名单':>4s}{'R5':>9s}{'超额5':>9s}"
          f"{'R10':>9s}{'超额10':>9s}{'R20':>9s}{'超额20':>9s}{'胜率20':>7s}")
    for p in periods:
        n = len(p["rows"])
        s = f"{p['S']:12s}{n:4d}"
        for h in HORIZONS:
            s += fmt_pct(mean_of([r[f"r{h}"] for r in p["rows"]]), 9)
            s += fmt_pct(mean_of([r[f"ex{h}"] for r in p["rows"]]), 9)
        vals = [r["ex20"] for r in p["rows"] if r["ex20"] is not None]
        win = (sum(1 for v in vals if v > 0) / len(vals) * 100) if vals else None
        s += f"{win:6.0f}%" if win is not None else "     -"
        print(s)
    print("-" * 78)
    print(f"{'合计':12s}{agg['n']:4d}", end="")
    for h in HORIZONS:
        print(fmt_pct(agg[f"r{h}"], 9) + fmt_pct(agg[f"ex{h}"], 9), end="")
    print(f"{agg['win20']:6.0f}%" if agg["win20"] is not None else "     -")
    print("\n说明：合计=全部入选样本等权平均；未扣交易成本；样本重叠，t 值仅参考。")


def print_attr(periods, h=20):
    rows = [r for p in periods for r in p["rows"]]
    print(f"\n分项（{h} 日超额，基准=同规模桶）：")
    def show(title, groups):
        print(f"  {title}")
        for name, sel in groups:
            vals = [r[f"ex{h}"] for r in sel if r[f"ex{h}"] is not None]
            if not vals:
                print(f"    {name:14s}  n=  0")
                continue
            win = sum(1 for v in vals if v > 0) / len(vals) * 100
            print(f"    {name:14s}  n={len(vals):3d}  超额{sum(vals)/len(vals)*100:+6.2f}%  "
                  f"胜率{win:3.0f}%")
    by_age = defaultdict(list)
    for r in rows:
        if r.get("age") is not None:
            by_age[int(r["age"])].append(r)
    show("按「距启动天数」", [(f"age={a}", by_age[a]) for a in sorted(by_age)])
    by_lim = defaultdict(list)
    for r in rows:
        by_lim[r["limit"] or "无数据"].append(r)
    show("按「启动日是否涨停」", [(k, by_lim[k]) for k in ("是", "接近", "否", "无数据") if by_lim[k]])
    by_q = defaultdict(list)
    for r in rows:
        q = r.get("turn20_rank")
        if q is None:
            by_q["无数据(v1.0)"].append(r)
        else:
            by_q[f"分位{int(q // 20) * 20}-{int(q // 20) * 20 + 20}"].append(r)
    keys = sorted(k for k in by_q if not k.startswith("无"))
    show("按「池内换手分位」", [(k, by_q[k]) for k in keys + (["无数据(v1.0)"] if "无数据(v1.0)" in by_q else [])])


# ---------------- picks_log.csv ----------------

def read_log(path):
    if not os.path.exists(path):
        return [], []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = list(csv.reader(fh))
    if not rd:
        return [], []
    return rd[0], rd[1:]


def write_log(path, header, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def update_log(path, periods):
    header, rows = read_log(path)
    if not header:
        header = list(MANAGED)
    else:
        for col in MANAGED:           # 兼容旧文件：缺列补到末尾
            if col not in header:
                header.append(col)
    idx = {c: i for i, c in enumerate(header)}
    table = {}
    for r in rows:
        r = r + [""] * (len(header) - len(r))
        key = (r[idx["日期"]], r[idx["代码"]])
        table[key] = r

    def cell(v):
        return "" if v is None else round(v, 4)

    added = updated = 0
    for p in periods:
        S = p["S"]
        for x in p["rows"]:
            key = (S, x["code"])
            r = table.get(key)
            if r is None:
                r = [""] * len(header)
                r[idx["日期"]] = S
                r[idx["代码"]] = x["code"]
                table[key] = r
                added += 1
            else:
                updated += 1
            for col, val in (
                ("名称", x["name"]), ("启动日", x["d_star"]), ("距启动", x["age"]),
                ("20日换手%", x["turn20"]), ("池内分位", x["turn20_rank"]),
                ("启动日涨停", x["limit"]),
                ("持有5日%", cell(x["r5"])), ("持有10日%", cell(x["r10"])),
                ("持有20日%", cell(x["r20"])),
                ("超额5日%", cell(x["ex5"])), ("超额10日%", cell(x["ex10"])),
                ("超额20日%", cell(x["ex20"])),
            ):
                if col in idx and val not in (None, ""):
                    r[idx[col]] = val
    out = [table[k] for k in sorted(table)]
    write_log(path, header, out)
    return added, updated, len(out)


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(description="选股名单前向评估（离线，只读 database/）")
    ap.add_argument("--db", default=DB, help="数据目录（默认项目根目录 database）")
    ap.add_argument("--log", default=LOG,
                    help="picks_log.csv 路径；传空字符串则不写")
    ap.add_argument("--no-log", action="store_true", help="不写 picks_log.csv")
    ap.add_argument("--json", help="把评估结果另存为 JSON")
    ap.add_argument("--fetch", action="store_true",
                    help="先补齐全市场日线再评估（需联网；约 4800 只、20 只/批，"
                         "实测 20–60 分钟，建议放后台）")
    ap.add_argument("--end", help="--fetch 的截止日（默认今天）")
    a = ap.parse_args()

    bars = load_bars(a.db)
    caps_by_day = load_caps_by_day(a.db)
    results = load_results(a.db)
    if not results:
        raise SystemExit(f"错误：{a.db} 下没有可评估的 result_*.json")
    if a.fetch:
        fetch_universe(a.db, bars, results, a.end)
        bars = load_bars(a.db)
    cal = sorted({d for ds, _ in bars.values() for d in ds})
    print(f"日线：{len(bars)} 只、{len(cal)} 个交易日（{cal[0]} ~ {cal[-1]}）")
    print(f"结果：{len(results)} 期（{results[0]['S']} ~ {results[-1]['S']}）")

    periods = []
    for r in results:
        rows, cover = eval_period(bars, cal, caps_by_day, a.db, r)
        if rows:
            periods.append({"S": r["S"], "version": r["version"],
                            "rows": rows, "cover": cover})

    all_rows = [x for p in periods for x in p["rows"]]
    agg = {"n": len(all_rows)}
    for h in HORIZONS:
        agg[f"r{h}"] = mean_of([x[f"r{h}"] for x in all_rows])
        agg[f"ex{h}"] = mean_of([x[f"ex{h}"] for x in all_rows])
    vals = [x["ex20"] for x in all_rows if x["ex20"] is not None]
    agg["win20"] = (sum(1 for v in vals if v > 0) / len(vals) * 100) if vals else None

    h_attr = next((h for h in (20, 10, 5)
                   if any(x[f"ex{h}"] is not None for x in all_rows)), 20)

    # 池内超额（独立于规模桶）
    for p in periods:
        for h in HORIZONS:
            p.setdefault("pool", {})[h] = mean_of(
                [(x[f"r{h}"] - x[f"pool{h}"]) for x in p["rows"]
                 if x.get(f"pool{h}") is not None and x[f"r{h}"] is not None])
    print_report(periods, agg)
    if periods:
        line = "  基准覆盖（S 与 S+h 都有成交价的股票数）："
        print(line + "  ".join(
            f"{p['S'][5:]}: " + "/".join(str(p['cover'].get(h, 0)) for h in HORIZONS)
            for p in periods))
    agg["cover_warn"] = any(
        (p["cover"].get(h, 0) < 2000) for p in periods for h in HORIZONS)

    if any(p["pool"].get(h_attr) is not None for p in periods):
        print(f"\n相对「当期粗筛池」的 {h_attr} 日超额（剔除规模/风格影响）：")
        for p in periods:
            if p["pool"].get(h_attr) is not None:
                print(f"  {p['S']}  n={len(p['rows']):3d}  "
                      f"{p['pool'][h_attr]*100:+6.2f}%")

    print_attr(periods, h_attr)

    if agg.get("cover_warn"):
        print("\n⚠️ 部分期的基准覆盖不足（见上），超额仅供参考。")
        print("   要得到可信的全市场基准，先补齐全市场日线：")
        print("     python3 evaluation/download_history.py --start <最早基准日> --end <最新日> --kline-only")
        print("   或直接：python3 evaluation/evaluate.py --fetch（自动补全市场日线，需联网）")

    pending = sum(1 for x in all_rows if x["r20"] is None)
    if pending:
        print(f"\n⚠️ {pending} 条尚无 20 日数据（最新几期），下轮评估会自动补齐。")

    if not a.no_log and a.log:
        added, updated, total = update_log(a.log, periods)
        print(f"\npicks_log.csv：新增 {added} 行、更新 {updated} 行，共 {total} 行 → {a.log}")

    if a.json:
        payload = {
            "生成时间": dt.datetime.now().isoformat(timespec="seconds"),
            "日线覆盖": [cal[0], cal[-1]],
            "期间": [{"基准日": p["S"], "版本": p["version"], "名单数": len(p["rows"]),
                      **{f"R{h}": mean_of([x[f"r{h}"] for x in p["rows"]]) for h in HORIZONS},
                      **{f"超额{h}": mean_of([x[f"ex{h}"] for x in p["rows"]]) for h in HORIZONS},
                      f"池内超额{h_attr}": p["pool"].get(h_attr)} for p in periods],
            "合计": agg,
            "明细": all_rows,
        }
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        print(f"JSON：{a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
