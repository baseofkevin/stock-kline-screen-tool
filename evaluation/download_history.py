#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史数据批量下载（供回测 / 统计用）

⚠️ 本程序只负责**下载并落盘**，不做任何判断或分析。
   下载完成后，由分析脚本读取 database/ 下的原始数据做重放统计。

用途：
    为「K线选股规则在各市值档的通过率是否公平」这个问题准备样本。
    区间默认 2026-06-01 ~ 2026-10-14（91 个交易日，可切出 18 个周频基准日）。

用法：
    python3 evaluation/download_history.py                          # 默认区间
    python3 evaluation/download_history.py --start 2026-06-01 --end 2026-10-14
    python3 evaluation/download_history.py --refresh                # 强制重下（已存在的不跳过）
    python3 evaluation/download_history.py --skip-kline             # 只下横截面与标签，不下K线

特性：
    · **可断点续传**：默认跳过已存在的文件，中断后重跑即可接着下
    · **失败即报错**：接口返回空列表会被判定为失败（历史日期静默返空是已知陷阱），
      重试仍失败则该阶段中止，**不会静默留下残缺数据**
    · 每步显示进度与预计剩余时间

产物（全部落在 database/，与 kline-screan.py 的命名保持一致，可被其直接复用）：
    cross_<交易日>.json        横截面粗筛原始返回              × 每个交易日
    labels_<交易日>.json       排除集（ST + 4 个次新分桶）      × 每个交易日
    kline_<起>_<止>.json       候选并集的日K线（**一个宽区间**）  × 1
    mv_<基准日>.json           流通市值/总市值 {code: [流通,总]}  × 每 5 日一个口径
    history_meta.json          本次下载清单与统计

退出码：0 正常 ｜ 1 出错（含下载失败）
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # kline-screan.py 与 database/ 都在项目根目录
DB = os.path.join(ROOT, "database")
W = os.path.expanduser("~/.local/bin/westock")

# ---- 规则常量直接从 kline_screen.params 取，保证下载口径与选股规则一致 ----
sys.path.insert(0, ROOT)
from kline_screen import params as RULE  # noqa: E402
from kline_screen.common.session import cache_final, session_final  # noqa: E402

CROSS_TPL = RULE.CROSS_TPL            # 横截面粗筛表达式（6 个条件，短表达式，历史日期可用）
NEW_LABELS = RULE.NEW_LABELS          # 4 个次新分桶
LABEL_PAGE = RULE.LABEL_PAGE          # 标签分页大小
KLINE_BATCH = RULE.KLINE_BATCH        # K线单批股票数
LOOKBACK_DAYS = RULE.KLINE_LOOKBACK_DAYS   # K线回溯自然日

SLEEP = 0.5                           # 每次调用后的间隔，避免触发本机限频器
RETRIES = 4

_T0 = time.time()
_DONE = [0]
_TOTAL = [0]


# ---------------- 基础工具 ----------------

def run(args, timeout=900):
    p = subprocess.run(args, capture_output=True, timeout=timeout)
    return p.stdout.decode("utf-8", "replace")


def md(txt):
    """解析 westock CLI 的 markdown 表格"""
    tbl = [l for l in txt.splitlines() if l.strip().startswith("|")]
    if len(tbl) < 2:
        return []
    hdr = [x.strip() for x in tbl[0].strip().strip("|").split("|")]
    out = []
    for ln in tbl[1:]:
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if len(c) == len(hdr) and not all(set(v) <= set("-: ") for v in c):
            out.append(dict(zip(hdr, c)))
    return out


def retry(fn, what, tries=RETRIES, backoff=(5, 15, 30, 60), allow_empty=False):
    """返回 None 表示彻底失败（调用方必须处理，不得静默继续）
    退避必须够长：实测瞬时故障可持续数秒，1.5/3/4.5s 的短退避扛不过去。

    allow_empty=True 时，空结果视为**成功但为空**（只用于「空结果本身合法」的
    场景，如标签分页的第 2 页起）；默认 False，因为历史日期接口会把失败静默
    表现为空列表。本参数与 kline-screan 的 retry() 保持同一语义。"""
    for i in range(tries):
        try:
            r = fn()
            if r or (allow_empty and r is not None):
                return r
        except Exception:
            pass
        if i < tries - 1:
            w = backoff[min(i, len(backoff) - 1)]
            sys.stderr.write(f"\n  [重试] {what} —— 第 {i+1} 次失败，等待 {w}s")
            sys.stderr.flush()
            time.sleep(w)
    sys.stderr.write(f"\n  [失败] {what}（重试 {tries} 次仍失败）\n")
    return None


def jdump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)          # 原子写入：中断不会留下半个文件


def jload(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def tick(label):
    """进度显示 + 预计剩余时间"""
    _DONE[0] += 1
    n, tot = _DONE[0], _TOTAL[0]
    el = time.time() - _T0
    eta = (el / n * (tot - n)) if n else 0
    sys.stdout.write(f"\r  [{n:4d}/{tot}] {label:<34s} 已用 {el/60:5.1f}min  剩余约 {eta/60:5.1f}min")
    sys.stdout.flush()


# ---------------- 各阶段下载 ----------------

def download_calendar(start, end, refresh):
    path = os.path.join(DB, "trading_calendar.json")
    cached = jload(path, {})
    allc = cached.get("days") or []
    # 缓存已覆盖整个区间就直接用，避免把已有日历缓存改窄
    if not refresh and allc and allc[0] <= start and allc[-1] >= end:
        return [d for d in allc if start <= d <= end]
    grid = RULE.get_trading_days(end, refresh, span=start)
    return [d for d in grid if start <= d <= end]


def download_cross(days, refresh):
    lo, hi = RULE.CHG_LO, RULE.CHG_HI
    expr = CROSS_TPL.format(lo=lo, hi=hi)
    _TOTAL[0] += len(days)
    for d in days:
        path = os.path.join(DB, f"cross_{d}.json")
        meta_path = os.path.join(DB, f"cross_{d}.meta.json")
        sig = RULE.cross_sig(lo, hi)
        meta = jload(meta_path, {})
        if (os.path.exists(path) and not refresh
                and meta.get("sig") == sig
                and cache_final(path, d, meta)):
            tick(f"粗筛 {d} (已有)")
            continue
        rows = retry(lambda: md(run([W, "screen", "condition", "--expression", expr,
                                     "--date", d, "--limit", "6000"])),
                     what=f"横截面粗筛 {d}")
        if rows is None:
            raise RuntimeError(f"横截面粗筛失败：{d}（不得留下残缺数据）")
        jdump(path, rows)
        # 与 kline-screan.py 的缓存签名保持一致，避免下载脚本写数据、
        # 选股脚本却因 meta 缺失/过期而重拉。
        jdump(meta_path, {"sig": sig,
                          "version": RULE.CROSS_CACHE_VERSION,
                          "expression": expr,
                          # 与选股主流程一致：记下这份粗筛是不是收盘后才抓的。
                          # 回补历史日期时恒为 True；只有「当天且未收盘」才是 False。
                          "final": session_final(d)})
        tick(f"粗筛 {d} ({len(rows)}只)")
        time.sleep(SLEEP)


def download_labels(days, refresh):
    _TOTAL[0] += len(days)
    for d in days:
        path = os.path.join(DB, f"labels_{d}.json")
        if os.path.exists(path) and not refresh:
            tick(f"标签 {d} (已有)")
            continue
        st, new = set(), set()
        for L in ["risk_st"] + NEW_LABELS:
            off = 0
            while True:
                rows = retry(lambda: md(run([W, "screen", "label", "--type", L,
                                             "--date", d, "--limit", str(LABEL_PAGE),
                                             "--offset", str(off)])),
                             what=f"标签 {L} @ {d}",
                             # 第 2 页起空页是「分页正常结束」，必须是成功；
                             # 否则某桶数量恰为 LABEL_PAGE 整数倍时会误判失败并中止整次下载。
                             allow_empty=(off > 0))
                if rows is None:
                    raise RuntimeError(f"排除集拉取失败：{L} @ {d}（不得留下残缺数据）")
                (st if L == "risk_st" else new).update(
                    r["code"] for r in rows if r.get("code"))
                if len(rows) < LABEL_PAGE:
                    break
                off += LABEL_PAGE
                time.sleep(0.3)
            time.sleep(0.3)
        jdump(path, {"st": sorted(st), "new": sorted(new)})
        tick(f"标签 {d} (ST{len(st)}/次新{len(new)})")
        time.sleep(SLEEP)


def download_mv(s_dates, refresh):
    """每个基准日拉一次全市场流通市值/总市值（市值比的分母）"""
    _TOTAL[0] += len(s_dates)
    for d in s_dates:
        path = os.path.join(DB, f"mv_{d}.json")
        if os.path.exists(path) and not refresh:
            tick(f"市值 {d} (已有)")
            continue
        rows = retry(lambda: md(run([W, "screen", "condition", "--expression",
                                     "intersect([NegotiableMV > 0, TotalMV > 0])",
                                     "--date", d, "--limit", "8000"])),
                     what=f"市值快照 {d}")
        if rows is None:
            raise RuntimeError(f"市值快照失败：{d}")
        # 只留 {code: [流通市值元, 总市值元]}，避免文件过大
        out = {}
        for r in rows:
            c = r.get("code")
            try:
                out[c] = [float(r["NegotiableMV"]), float(r["TotalMV"])]
            except (KeyError, TypeError, ValueError):
                continue
        jdump(path, out)
        tick(f"市值 {d} ({len(out)}只)")
        time.sleep(SLEEP)


def _fetch_one(code, beg, end):
    """单只拉取（补取用）——必须解析**全部**行，不能只取 rows[0】
    补取用**短退避**：整批失败时接口通常正忙，长退避会让一批磨十几分钟。"""
    rows = retry(lambda: md(run([W, "kline", code, "--start", beg, "--end", end,
                                 "--limit", "400"])),
                 what=f"补取 {code}", tries=2, backoff=(5, 10))
    if not rows:
        return None
    out = []
    for r in rows:
        try:
            out.append(RULE.to_bar(r))
        except (KeyError, ValueError):
            continue
    return sorted(out, key=lambda b: b[0]) if out else None


def download_kline(codes, beg, end, refresh, batch=KLINE_BATCH, flush_every=10):
    path = os.path.join(DB, f"kline_{beg}_{end}.json")
    # ⚠️ 必须排序：接口按**日期降序**（新→旧）返回，而下游 find_start / MA 全部假定升序。
    #    漏掉这一步会让所有均线与启动日计算方向颠倒（已踩过，整个回测作废）。
    km = {} if refresh else {c: sorted(v, key=lambda b: b[0])
                             for c, v in jload(path, {}).items()}
    todo = [c for c in codes if c not in km]
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    _TOTAL[0] += max(len(chunks), 1)
    n_fallback = 0
    consec_fail = 0
    for n, ch in enumerate(chunks, 1):
        rows = retry(lambda: md(run([W, "kline", ",".join(ch), "--start", beg,
                                     "--end", end, "--limit", "400"])),
                     what=f"日K线 第{n}/{len(chunks)}批（{len(ch)}只）",
                     tries=3, backoff=(5, 15, 30))
        if rows:
            consec_fail = 0
            touched = set()
            for r in rows:
                c = r.get("code") or (ch[0] if len(ch) == 1 else None)
                if not c:
                    continue
                try:
                    km.setdefault(c, []).append(RULE.to_bar(r))
                    touched.add(c)
                except (KeyError, ValueError):
                    continue
            for c in touched:                  # 每只单独排回升序
                km[c].sort(key=lambda b: b[0])
        else:
            # 整批失败 -> 逐只补取（短退避），并**立即落盘**，绝不因一批失败中止整次下载
            consec_fail += 1
            n_fallback += 1
            sys.stderr.write(f"\n  第{n}批整体失败，逐只补取 {len(ch)} 只 ...")
            sys.stderr.flush()
            for c in ch:
                got = _fetch_one(c, beg, end)
                if got:
                    km[c] = got
                time.sleep(0.3)
            jdump(path, km)
            sys.stderr.write(f" 完成（累计 {len(km)} 只）\n")
            if consec_fail >= 5:
                sys.stderr.write(
                    f"\n  ⚠️ 连续 {consec_fail} 批整体失败，接口疑似不可用，先停下。"
                    f"\n     已完成 {len(km)}/{len(codes)} 只，重跑同一命令可续。\n")
                break
        if n % flush_every == 0 or n == len(chunks):
            jdump(path, km)
        tick(f"日K线 第{n}/{len(chunks)}批")
        time.sleep(0.4)
    jdump(path, km)
    # 收尾：仍缺的再补一轮
    still = [c for c in codes if c not in km]
    if still:
        sys.stderr.write(f"\n  收尾补取 {len(still)} 只 ...\n")
        for i, c in enumerate(still, 1):
            got = _fetch_one(c, beg, end)
            if got:
                km[c] = got
            if i % 20 == 0:
                jdump(path, km)
                sys.stderr.write(f"\r    收尾 {i}/{len(still)}，已补 {len(km)} 只")
                sys.stderr.flush()
            time.sleep(0.3)
        jdump(path, km)
        sys.stderr.write("\n")
    # 校验：接口在个别批次会**只返回前 400 行**（状态仍显示 success），
    #       导致一批里约 10 只股票各只有 ~40 根K线（末日远早于 end）。
    #       这类必须重取，否则下游会算错均线。
    trunc = [c for c in codes if km.get(c) and km[c][-1][0] != end]
    if trunc:
        sys.stderr.write(f"\n  校验：发现 {len(trunc)} 只数据被截断（末日≠{end}），重取 ...\n")
        fixed = 0
        for i, c in enumerate(trunc, 1):
            got = _fetch_one(c, beg, end)
            if got and len(got) > len(km.get(c, [])):
                km[c] = got
                fixed += 1
            if i % 20 == 0:
                jdump(path, km)
                sys.stderr.write(f"\r    重取 {i}/{len(trunc)}，已修 {fixed}")
                sys.stderr.flush()
            time.sleep(0.3)
        jdump(path, km)
        # 重取后仍缺最近数据的，多半是停牌，属正常，如实报出
        left = [c for c in trunc if km.get(c) and km[c][-1][0] != end]
        sys.stderr.write(f"\n    修好 {fixed} 只；仍缺最近数据 {len(left)} 只"
                         f"（多为停牌，属正常）\n")
    if n_fallback:
        sys.stderr.write(f"\n  （本次有 {n_fallback} 批整体失败，已用逐只补取兜底）\n")
    return km, [c for c in codes if c not in km]


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="历史数据批量下载（只下载，不分析）")
    ap.add_argument("--start", default="2026-06-01", help="起始交易日 YYYY-MM-DD")
    ap.add_argument("--end", default="2026-10-14", help="结束交易日 YYYY-MM-DD")
    ap.add_argument("--refresh", action="store_true", help="强制重下（不跳过已有文件）")
    ap.add_argument("--skip-kline", action="store_true", help="跳过 K 线下载")
    ap.add_argument("--kline-only", action="store_true",
                    help="只补下载 K 线（横截面/标签/市值视为已完成）——断点续传用")
    ap.add_argument("--kline-batch", type=int, default=KLINE_BATCH,
                    help=f"K线单批股票数（默认 {KLINE_BATCH}；网络不稳可调小）")
    a = ap.parse_args()

    if not os.path.exists(W):
        print(f"错误：找不到 westock CLI（{W}）", file=sys.stderr)
        return 1
    os.makedirs(DB, exist_ok=True)

    print("=" * 78)
    print("历史数据批量下载   区间：%s ~ %s" % (a.start, a.end))
    print("=" * 78)

    print("\n[1/5] 交易日历 ...")
    days = download_calendar(a.start, a.end, a.refresh)
    if not days:
        print("错误：区间内无交易日", file=sys.stderr)
        return 1
    # 周频基准日：从区间末尾每 5 个交易日取一个，保证最后一个 = --end
    s_dates = [days[i] for i in range(len(days) - 1, -1, -5)]
    s_dates = sorted(s_dates)
    print(f"      交易日 {len(days)} 个（{days[0]} ~ {days[-1]}）")
    print(f"      周频基准日 {len(s_dates)} 个：{', '.join(s_dates)}")

    # 需要 5 日窗口的最后一个基准日不能太靠前
    s_dates = [d for d in s_dates if days.index(d) >= 4]
    print(f"      其中窗口完整的 {len(s_dates)} 个（其余日期不足 5 日窗口，已剔除）")

    pend_c = len([d for d in days if not os.path.exists(os.path.join(DB, f"cross_{d}.json"))])
    pend_l = len([d for d in days if not os.path.exists(os.path.join(DB, f"labels_{d}.json"))])
    pend_m = len([d for d in s_dates if not os.path.exists(os.path.join(DB, f"mv_{d}.json"))])
    print(f"      待下载：粗筛 {pend_c} 项，标签 {pend_l} 项，市值 {pend_m} 项"
          f"（已有文件自动跳过；进度条按「总处理项」计，含跳过项）")
    print("      已有文件会复用：database/cross_*.json、labels_*.json、mv_*.json、kline_*.json")

    if a.kline_only:
        print("\n[--kline-only] 跳过横截面 / 标签 / 市值，只补 K 线")
        # 从已有 cross 文件重建候选并集，避免重复遍历
        # 只认 cross_<YYYY-MM-DD>.json，避免把 cross_<d>.meta.json 签名文件
        # 当成交易日，随后把 meta 的 dict 当 list 遍历 → AttributeError。
        import glob
        days = sorted({m.group(1) for f in glob.glob(os.path.join(DB, "cross_*.json"))
                       if (m := re.match(r"cross_(\d{4}-\d{2}-\d{2})\.json$",
                                         os.path.basename(f)))})
        if not days:
            print("错误：database/ 下没有 cross_*.json，无法确定候选并集", file=sys.stderr)
            return 1
        print(f"      依据已有 {len(days)} 个 cross 文件（{days[0]} ~ {days[-1]}）")
    else:
        print("\n[2/5] 横截面粗筛 ...")
        download_cross(days, a.refresh)
        print()

        print("\n[3/5] 排除集（ST + 次新分桶）...")
        download_labels(days, a.refresh)
        print()

    # 候选并集（供 K 线一次性拉取）
    pool = set()
    for d in days:
        rows = jload(os.path.join(DB, f"cross_{d}.json"), [])
        if not isinstance(rows, list):      # 非 list 直接跳过，避免损坏文件导致 AttributeError
            print(f"  [跳过] cross_{d}.json 不是列表（疑似损坏/非粗筛文件）",
                  file=sys.stderr)
            continue
        for r in rows:
            if isinstance(r, dict) and r.get("code"):
                pool.add(r["code"])
    pool = sorted(pool)
    print(f"\n[4/5] 候选并集 {len(pool)} 只（跨 {len(days)} 个交易日）")

    km_missing = []
    if a.skip_kline:
        print("      --skip-kline：已跳过")
    else:
        kbeg = (dt.date.fromisoformat(days[0])
                - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
        kend = days[-1]
        print(f"      K线区间 {kbeg} ~ {kend}（{len(pool)} 只，约 "
              f"{(len(pool) + KLINE_BATCH - 1)//KLINE_BATCH} 批）")
        km, km_missing = download_kline(pool, kbeg, kend, a.refresh, a.kline_batch)
        print(f"\n      K线命中 {sum(1 for c in pool if km.get(c))}/{len(pool)}"
              + (f"，缺 {len(km_missing)} 只" if km_missing else ""))

    print("\n[5/5] 基准日市值快照（市值比的分母）...")
    if a.skip_kline:
        print("      --skip-kline：已跳过")
    else:
        download_mv(s_dates, a.refresh)
    print()

    # ---- 汇总 ----
    def size_of(d):
        return sum(os.path.getsize(os.path.join(DB, f))
                   for f in os.listdir(DB) if f.endswith(".json"))

    meta = {
        "下载时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "区间": [a.start, a.end],
        "交易日数": len(days),
        "交易日": days,
        "周频基准日": s_dates,
        "候选并集数": len(pool),
        "候选并集": pool,
        "K线缺失": km_missing,
        "数据库总大小MB": round(size_of(DB) / 1024 / 1024, 1),
    }
    jdump(os.path.join(DB, "history_meta.json"), meta)

    el = time.time() - _T0
    print("=" * 78)
    print("下载完成")
    print("=" * 78)
    print(f"  交易日        {len(days)} 个（{days[0]} ~ {days[-1]}）")
    print(f"  周频基准日    {len(s_dates)} 个")
    print(f"  候选并集      {len(pool)} 只")
    print(f"  K线缺失       {len(km_missing)} 只" + (f" {km_missing[:8]}" if km_missing else ""))
    print(f"  database/     {meta['数据库总大小MB']} MB")
    print(f"  用时          {el/60:.1f} 分钟")
    print(f"\n  清单已写入 {os.path.join(DB, 'history_meta.json')}")
    print("  接下来把 history_meta.json 的内容发我，我来做重放统计。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
