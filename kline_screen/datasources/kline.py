"""K 线缓存、分批抓取与 bar 兼容读取。"""
import datetime as dt
import os
import re
import sys
import time

from ..config import DB, W
from ..common.storage import jdump, jload
from ..common.values import fnum
from ..common.westock import md, retry, run
from ..params import (KLINE_BAD_LIMIT, KLINE_BATCH, KLINE_DELTA_BATCH,
                      KLINE_FLUSH_EVERY, KLINE_MAX_BARS, KLINE_PACE_MAX,
                      KLINE_PACE_MIN, KLINE_RETRY_BATCH, MIN_HISTORY)


BAR_ORDER = "date,close,volume,open,high,low,amount"


def bar_at(bar, idx):
    """安全读取 bar 的第 idx 位。

    ⚠️ database/kline_*.json 里既有 3 元组，也有 7 元组。对 3 元组取
    open/high/low/amount 会 IndexError，因此任何 OHLC 读取都必须走本函数，
    否则会在旧缓存上静默失效或直接崩。
    """
    return bar[idx] if len(bar) > idx else None


def bar_low(bar):
    return bar_at(bar, 5)


def bar_high(bar):
    return bar_at(bar, 4)


def to_bar(r):
    """westock kline 行 → (date, close, volume, open, high, low, amount)。

    ⚠️ 顺序把 close/volume 放在第 2/3 位，以兼容已有的 3 元组缓存；
    旧缓存没有后 4 位时按 None 处理。新增字段只用于后续的质量过滤，
    不改变 find_start/judge 的现有口径。
    """
    return (r["date"], float(r["last"]), float(r["volume"]),
            fnum(r.get("open")), fnum(r.get("high")),
            fnum(r.get("low")), fnum(r.get("amount")))


def _kline_bars(rows, chunk):
    """把一批接口行按 code 分组为**升序** bar 列表；解析失败的行走掉。

    ⚠️ 接口按日期降序（新→旧）返回，而 find_start / MA 全部假定升序，
       所以这里必须排回升序，不能直接沿用返回顺序。
    """
    out = {}
    for r in rows:
        c = r.get("code") or (chunk[0] if len(chunk) == 1 else None)
        if not c:
            continue
        try:
            out.setdefault(c, []).append(to_bar(r))
        except (KeyError, ValueError):
            continue
    for v in out.values():
        v.sort(key=lambda b: b[0])
    return out


def _kline_batch(ch, beg, end, tries=3, allow_empty=False):
    """取一批（≤batch 只）日 K 行；彻底失败返回 None。

    allow_empty=True 用于「增量补新」：这批股票可能整段都在停牌，接口如实返回空表，
    此时空表是**合法结果**（停牌），不是失败。若当成失败，一批全停牌的票会连试 3 次、
    还会被 `bad` 计数误判成「接口不可用」。
    """
    return retry(lambda: md(run([W, "kline", ",".join(ch), "--start", beg,
                                 "--end", end, "--limit", "400"])),
                 tries=tries, base=2.0, allow_empty=allow_empty,
                 what=f"日K线（{len(ch)} 只，{ch[0]} 起）")


def _kline_files():
    """database/ 下所有 kline_<起>_<止>.json，按截止日新→旧。"""
    out = []
    if not os.path.isdir(DB):
        return out
    for name in os.listdir(DB):
        m = re.match(r"kline_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$", name)
        if m:
            out.append((m.group(2), name, os.path.join(DB, name)))
    out.sort(reverse=True)
    return out


def _load_kline_base(path, refresh):
    """K 线底稿 {code: bars}。refresh 时为空（=全量重下）。

    优先用同名缓存（说明这段区间已拉全），否则借用最新一份 kline_*.json 当底：
    底稿里每只股票的**最后一根日期就是增量起点**，这样基准日往前挪一天时，
    只需要补每只新增的那一两根，而不是重下整段历史。
    """
    if refresh:
        return {}
    if os.path.exists(path):
        return jload(path, {})
    for _end, name, p in _kline_files():
        if os.path.abspath(p) == os.path.abspath(path):
            continue
        km = jload(p, {})
        if km:
            sys.stderr.write(f"  复用历史底稿 {name}（只补新增交易日）\n")
            return km
    return {}


def _merge_bars(km, got, cap=KLINE_MAX_BARS):
    """把新取的 bars 并入 km：按日期去重（新值覆盖旧值）并裁到最近 cap 根。"""
    for c, bars in got.items():
        cur = km.setdefault(c, [])
        if cur and bars:
            merged = {b[0]: b for b in cur}
            merged.update({b[0]: b for b in bars})
            cur[:] = [merged[d] for d in sorted(merged)]
        elif bars:
            cur.extend(bars)
            cur.sort(key=lambda b: b[0])
        if cap and len(cur) > cap:
            del cur[:len(cur) - cap]


def get_kline(codes, beg, end, refresh, require_codes=None):
    """拉取候选并集的日 K，并保证「基准日有成交的股票」一定有 end 日 K。

    bar 落盘为 7 元组；落盘文件仍是 kline_<起>_<止>.json（格式不变，evaluate.py
    与 download_history.py 照旧可用）。require_codes（通常是 cross[end] 的代码）
    重取后仍缺 end 日 K 时直接报错，不以「K线无 S 数据」静默剔除。

    性能要点（2026-11-08）：
      · **增量取数**：以最新一份 kline_*.json 为底稿，每只只补「最后一根之后」
        新增的交易日。基准日往前挪一天时，4996 只的日增量约 5000 行，而不是
        95 万行。`--refresh` 或换股票池才会走全量。
      · **落盘节流**：原先每批都 jdump 整个 km。km 95 万根（62MB）时单次 json.dump
        13.2 秒，169 批累计约 17 分钟，是第 4 步 80% 以上的耗时。现每
        KLINE_FLUSH_EVERY 批落一次 + 收尾一次（与 download_history 的 flush_every 同）。
      · **补取 / 修数据改小批**：原来「缺 1 只 → 请求 1 只 → 全量落盘一次」，
        334 只缺失 ≈ 72 分钟纯写盘；现按 KLINE_RETRY_BATCH 分组，每轮落一次盘。
      · **批大小**：全量回补 20 只/批（服务端吞吐约 600 只/分钟，50 只/批*持续*拉
        会被拖到 ~10s/批）；增量补新只有几行/只，用 KLINE_DELTA_BATCH=60。
        按返回完整度自适应：整批回全就把间隔乘 0.7 提速，丢码就翻倍减速。
      · 单批整体失败不再中止整次运行（该批的码留给补取），连续 KLINE_BAD_LIMIT 批
        失败才判接口不可用。真正的门禁仍是收尾的 require_codes 缺失检查。
    """
    require_codes = set(require_codes or ())
    path = os.path.join(DB, f"kline_{beg}_{end}.json")
    base = _load_kline_base(path, refresh)
    pool = set(codes)
    # 只保留本次候选；统一成升序；并**截掉晚于 end 的 K 线**。
    # 截断这一步是必须的：回看旧日期（--date 早于底稿的截止日）时，底稿里的"未来"
    # K 线会让「末日≠end」把每只股票都判成未更新，require_codes 检查会直接报错。
    km = {}
    for c, v in base.items():
        if c not in pool or not v:
            continue
        bars = sorted(v, key=lambda b: b[0])
        if bars[-1][0] > end:
            bars = [b for b in bars if b[0] <= end]
        if bars:
            km[c] = bars

    # 没有历史 → 从 beg 全量回补；有历史但落后 → 从「最后一根 + 1 天」增量补新
    full = [c for c in codes if not km.get(c)]
    delta = {}
    for c in codes:
        bars = km.get(c)
        if bars and bars[-1][0] < end:
            nxt = (dt.date.fromisoformat(bars[-1][0])
                   + dt.timedelta(days=1)).isoformat()
            delta.setdefault(nxt, []).append(c)
    work = []
    if full:
        work.append((beg, full, KLINE_BATCH, "全量回补", False))
    for start, group in sorted(delta.items()):
        # 增量批次允许「空返回」：这一批可能全是停牌股，空表是如实结果
        work.append((start, group, KLINE_DELTA_BATCH, "增量补新", True))
    up_to_date = len(codes) - len(full) - sum(len(g) for g in delta.values())
    if up_to_date:
        sys.stderr.write(f"  已有 {up_to_date} 只更新到 {end}，跳过\n")

    pace = KLINE_PACE_MIN
    bad = 0
    for wi, (start, group, bsz, label, may_empty) in enumerate(work, 1):
        chunks = [group[i:i + bsz] for i in range(0, len(group), bsz)]
        sys.stderr.write(f"  [{wi}/{len(work)}] {label}：{len(group)} 只"
                         f"（{len(chunks)} 批 × ≤{bsz} 只，{start} ~ {end}）\n")
        for n, ch in enumerate(chunks, 1):
            rows = _kline_batch(ch, start, end, allow_empty=may_empty)
            if rows is None:
                bad += 1
                sys.stderr.write(f"\n  第 {label} {n}/{len(chunks)} 批整体失败"
                                 f"（{len(ch)} 只），留给补取\n")
                if bad >= KLINE_BAD_LIMIT:
                    raise RuntimeError(
                        f"日K线连续 {bad} 批失败，接口疑似不可用，"
                        f"终止以免产出不可信名单")
            else:
                bad = 0
                got = _kline_bars(rows, ch)
                _merge_bars(km, got)
                if not got and may_empty:
                    pass                                        # 整批停牌，正常
                elif len(got) >= len(ch):
                    pace = max(KLINE_PACE_MIN, pace * 0.7)      # 完整 → 慢慢提速
                else:
                    pace = min(KLINE_PACE_MAX, pace * 2 + 0.2)  # 丢码 → 减速
                    sys.stderr.write(f"\n  {label} 第 {n} 批只回 {len(got)}/{len(ch)} 只"
                                     f"（服务端限频），节流到 {pace:.1f}s\n")
            sys.stderr.write(f"\r  {label} {n}/{len(chunks)} 批  库内 {len(km)} 只")
            sys.stderr.flush()
            if KLINE_FLUSH_EVERY and n % KLINE_FLUSH_EVERY == 0:
                jdump(path, km)
            time.sleep(pace)
        sys.stderr.write("\n")
        if wi < len(work):      # 最后一批不做中途落盘，收尾还会写一次
            jdump(path, km)

    # 批量拉取时单只失败会静默跳过（限频、接口部分失败），因此按小批补取；
    # 小批还不行就退到逐只。
    # ⚠️ 补取必须写入**整段历史**：若只写基准日一根，find_start 的 i 恒为 0，
    #    会被误判「K线历史不足」；坏结果还会被 jdump 缓存，后续重跑不会自动修复。
    for rnd, bs in enumerate((KLINE_RETRY_BATCH, KLINE_RETRY_BATCH, 1, 1)):
        missing = [c for c in codes if not km.get(c)]
        if not missing:
            break
        groups = [missing[i:i + bs] for i in range(0, len(missing), bs)]
        sys.stderr.write(f"  补取缺失 {len(missing)} 只（第 {rnd + 1} 轮，"
                         f"{len(groups)} 批 × {bs} 只）...\n")
        before = set(km)
        for gi, grp in enumerate(groups, 1):
            rows = _kline_batch(grp, beg, end, tries=2)
            _merge_bars(km, _kline_bars(rows or [], grp))
            left = sum(1 for c in codes if not km.get(c))
            sys.stderr.write(f"\r    补取 {gi}/{len(groups)} 批，仍缺 {left} 只")
            sys.stderr.flush()
            time.sleep(0.3)
        jdump(path, km)
        sys.stderr.write("\n")
        for c in sorted(set(km) - before):
            if len(km[c]) < MIN_HISTORY:
                sys.stderr.write(
                    f"\n  [警告] {c} 补取仅 {len(km[c])} 根 K 线"
                    f"（需 ≥{MIN_HISTORY} 根）：若它本应入选，多半是取数不全\n")

    # ① 历史明显偏短、且末日远早于 end 的：多半被接口「只返回前 400 行」截断，
    #    重取整段历史。真正的新股（末日=end）即使根数少也不动，免得每天白跑。
    short = [c for c in codes
             if km.get(c) and len(km[c]) < MIN_HISTORY and km[c][-1][0] != end]
    if short:
        sys.stderr.write(f"\n  校验：{len(short)} 只历史偏短且末日≠{end}，重取整段 ...\n")
        for i in range(0, len(short), KLINE_RETRY_BATCH):
            grp = short[i:i + KLINE_RETRY_BATCH]
            rows = _kline_batch(grp, beg, end, tries=2)
            for c, bars in _kline_bars(rows or [], grp).items():
                if bars[-1][0] == end and len(bars) >= len(km.get(c, [])):
                    km[c] = bars
            time.sleep(0.3)
        jdump(path, km)

    # ② 基准日有成交（require_codes）却仍没有 end 日 K：必须重取整段，仍缺就报错，
    #    不能把它当成停牌、用「K线无 S 数据」静默剔除。
    stale_req = sorted(c for c in codes
                       if c in require_codes and km.get(c) and km[c][-1][0] != end)
    if stale_req:
        sys.stderr.write(f"\n  校验：{len(stale_req)} 只在 {end} 有成交但 K 线未到"
                         f"{end}，重取整段 ...\n")
        for i in range(0, len(stale_req), KLINE_RETRY_BATCH):
            grp = stale_req[i:i + KLINE_RETRY_BATCH]
            rows = _kline_batch(grp, beg, end, tries=2)
            for c, bars in _kline_bars(rows or [], grp).items():
                if bars[-1][0] == end and len(bars) >= len(km.get(c, [])):
                    km[c] = bars
            time.sleep(0.3)
        jdump(path, km)
        left = [c for c in stale_req if km.get(c) and km[c][-1][0] != end]
        if left:
            raise RuntimeError(
                f"K线未更新且重取失败：{left[:8]}"
                f"{'…' if len(left) > 8 else ''} 在 {end} 有成交，"
                f"不能按停牌处理。请加 --refresh 或检查接口。")

    # ③ 其余末日≠end 的（多为停牌）如实提示，按「无 end 数据」剔除
    stale = [c for c in codes if km.get(c) and km[c][-1][0] != end]
    if stale:
        sys.stderr.write(
            f"  [警告] {len(stale)} 只 K 线末日≠{end}（多为停牌），"
            f"将按「无 {end} 数据」剔除\n")

    for k in km:            # 兜底：保证全库升序（0.1 秒，换一次不变式检查）
        km[k].sort(key=lambda b: b[0])
    jdump(path, km)

    missing = [c for c in codes if not km.get(c)]
    critical_missing = sorted(set(missing) & require_codes)
    if critical_missing:
        raise RuntimeError(
            f"K线完全缺失：{critical_missing[:8]}"
            f"{'…' if len(critical_missing) > 8 else ''} 在 {end} 有成交，"
            f"不能按停牌处理。请加 --refresh 或检查接口。")
    return km, missing
