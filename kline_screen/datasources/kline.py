"""K 线缓存、分批抓取与 bar 兼容读取。"""
import datetime as dt
import os
import re
import sys
import time

from ..config import DB, W
from ..common.session import MARKET_CLOSE, cache_final, session_final
from ..common.storage import jdump, jload
from ..common.values import fnum
from ..common.westock import md, retry, run
from ..params import (KLINE_BACKOFF_BASE, KLINE_BACKOFF_MAX, KLINE_BAD_LIMIT,
                      KLINE_BATCH, KLINE_DELTA_BATCH, KLINE_FLUSH_EVERY,
                      KLINE_KEEP, KLINE_MAX_BARS, KLINE_PACE_MAX, KLINE_PACE_MIN,
                      KLINE_RETRY_BATCH, KLINE_SPLIT_DEPTH, KLINE_SPLIT_MIN,
                      KLINE_STALE_ROUNDS, KLINE_WARN_FILES, MIN_HISTORY)


BAR_ORDER = "date,close,volume,open,high,low,amount"


def bar_at(bar, idx):
    """安全读取 bar 的第 idx 位。

    ⚠️ database/kline_*.json 里既有 3 元组，也有 7 元组。对 3 元组取
    open/high/low/amount 会 IndexError，因此任何 OHLC 读取都必须走本函数，
    否则会在旧缓存上静默失效或直接崩。
    """
    return bar[idx] if len(bar) > idx else None


def bar_low(bar):
    """当根最低价；3 元组旧缓存返回 None（为将来的质量过滤预留，当前不参与裁决）。"""
    return bar_at(bar, 5)


def bar_high(bar):
    """当根最高价；3 元组旧缓存返回 None（同上，预留）。"""
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


def _kline_fetch(ch, beg, end, tries=3, allow_empty=False, depth=0):
    """取一批日 K，**丢码时当场把缺失的拆半再取**，最多拆 KLINE_SPLIT_DEPTH 层。

    为什么不等：服务端限频的表现不是「整批失败」，而是「60 只只回 10 只」——
    单次返回条数被压掉一个数量级。此时等几十秒再发同样的 60 只请求，往往还是回
    10 只（2026-11-10 实测连续 6 批都是 10/60）；而拆成 10 只一批，一次就全回。
    所以这里不靠等待，靠**减小单次请求的规模**把数据拿全。

    返回 {code: bars}；整批彻底失败返回 None（调用方按接口故障处理）。
    """
    rows = _kline_batch(ch, beg, end, tries=tries, allow_empty=allow_empty)
    if rows is None:
        return None
    got = _kline_bars(rows, ch)
    missing = [c for c in ch if c not in got]
    if not missing or depth >= KLINE_SPLIT_DEPTH or len(ch) <= KLINE_SPLIT_MIN:
        return got
    half = max(KLINE_SPLIT_MIN, len(missing) // 2)
    for i in range(0, len(missing), half):
        sub = missing[i:i + half]
        sub_got = _kline_fetch(sub, beg, end, tries=tries,
                               allow_empty=allow_empty, depth=depth + 1)
        if sub_got:
            got.update(sub_got)
    return got


def _traded_on(codes, day):
    """这些票在 day 到底有没有成交 —— 用来区分「限频漏取」和「真停牌」。

    只问行情快照（每批 20 只、带 --date），不回 K 线：停牌股快照里 volume=0。
    返回 {code: 是否成交}，**只包含真正核对到的票**；核对不到的码不出现在结果里，
    调用方据此判断结论是否完整（不能把「没拿到快照」当成「没成交」）。
    """
    from .market import get_quotes        # 延迟导入，避免 datasources 包内循环
    out = {}
    try:
        snap = get_quotes(list(codes), day, False) or {}
    except Exception:                     # 行情接口抖动不应把整次运行拖死
        return out
    for c in codes:
        row = snap.get(c)
        if row is None:
            continue
        try:
            out[c] = float(row.get("volume") or 0) > 0
        except (TypeError, ValueError):
            continue
    return out


def _kline_files():
    """database/ 下所有 kline_<起>_<止>.json，按截止日新→旧。

    注意正则锚定在第二个日期后紧跟 `.json`，因此 kline_<起>_<止>.meta.json
    不会被误当成底稿。
    """
    out = []
    if not os.path.isdir(DB):
        return out
    for name in os.listdir(DB):
        m = re.match(r"kline_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$", name)
        if m:
            out.append((m.group(2), name, os.path.join(DB, name)))
    out.sort(reverse=True)
    return out


def _kline_meta(path):
    """kline_<起>_<止>.json → kline_<起>_<止>.meta.json（同目录旁挂）。"""
    return path[:-5] + ".meta.json" if path.endswith(".json") else path + ".meta.json"


def prune_kline_files(keep=KLINE_KEEP, protect=None):
    """只保留最近 keep 份 kline 底稿（连同 .meta.json），返回被删文件名列表。

    ⚠️ 只在显式加 `--prune-kline` 时调用。每次运行都会新建一份
    `kline_<新起>_<S>.json`，单份约 65 MB，跑十几期就是几个 GB。

    能安全删除的前提是「新底稿 ⊇ 旧底稿」：get_kline 落盘时保留底稿里的全部
    代码（不按当期候选裁剪），并且起的 = need[0] − 300 天、止 = S，所以新文件
    的区间与股票集合都覆盖旧的。protect 传本次刚写完的文件，永不删。
    """
    files = _kline_files()                      # 新 → 旧
    protected = {os.path.abspath(protect)} if protect else set()
    protected |= {os.path.abspath(p) for _, _, p in files[:keep]}
    removed = []
    for _, name, path in files:
        if os.path.abspath(path) in protected:
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        meta = _kline_meta(path)
        if os.path.exists(meta):
            try:
                os.remove(meta)
            except OSError:
                pass
        removed.append(name)
    return removed


def kline_files_hint(limit=KLINE_WARN_FILES):
    """底稿份数过多时的一行提示（不删任何文件，只提醒）。"""
    files = _kline_files()
    if len(files) < limit:
        return None
    size = 0
    for _, _, path in files:
        try:
            size += os.path.getsize(path)
        except OSError:
            pass
    return (f"database/ 下有 {len(files)} 份 kline 底稿（约 {size / 1e6:.0f} MB），"
            f"可加 --prune-kline 只保留最新 {KLINE_KEEP} 份")


def _flush(path, km, end):
    """落盘 K 线，并记下这份底稿是「哪一天收盘定稿」写出来的。

    meta 里的 final 是防「盘中半成品 bar 被永久留下」的：增量逻辑只要
    「最后一根日期 == end」就认为这只已更新，不会再取；若不记录定稿状态，
    盘中抓的那根（收盘价/成交量都是半成品）会一直留在库里，之后所有重跑
    都拿它当终值，且没有任何提示。
    """
    jdump(path, km)
    jdump(_kline_meta(path),
          {"end": end, "final": session_final(end),
           "saved_at": dt.datetime.now().isoformat(timespec="seconds")})


def _drop_unfinal_tail(km, path, end):
    """底稿写于 end 的收盘前 → 丢掉 end 那根 bar，让它被重新补取。

    只动「最后一根正好是 end」的票；历史日（end 早于写入日）不受影响。
    """
    if not km or not end:
        return km
    meta = jload(_kline_meta(path), {})
    if cache_final(path, end, meta):
        return km
    n = 0
    for bars in km.values():
        if bars and bars[-1][0] == end:
            bars.pop()
            n += 1
    if n:
        sys.stderr.write(
            f"  [警告] 底稿 {os.path.basename(path)} 写于 {end} 收盘前，"
            f"已丢弃 {n} 只当日的半成品 K 线，将重新补取\n")
    return km


def _load_kline_base(path, refresh, end):
    """K 线底稿 {code: bars}。refresh 时为空（=全量重下）。

    优先用同名缓存（说明这段区间已拉全），否则借用最新一份 kline_*.json 当底：
    底稿里每只股票的**最后一根日期就是增量起点**，这样基准日往前挪一天时，
    只需要补每只新增的那一两根，而不是重下整段历史。
    """
    if refresh:
        return {}
    if os.path.exists(path):
        return _drop_unfinal_tail(jload(path, {}), path, end)
    for f_end, name, p in _kline_files():
        if os.path.abspath(p) == os.path.abspath(path):
            continue
        km = jload(p, {})
        if km:
            sys.stderr.write(f"  复用历史底稿 {name}（只补新增交易日）\n")
            return _drop_unfinal_tail(km, p, f_end)
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

    返回值 km 含**底稿里的全部代码**（不止本次候选），这样后一期底稿总是前一期
    的超集，清理旧底稿不会让历史个股凭空消失。调用方按自己的 codes 过滤即可。

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
    base = _load_kline_base(path, refresh, end)
    # 底稿**不按本次候选裁剪**：候选池每周在变（股票离开池子就会被裁掉），
    # 若只把本次 pool 写进新文件，每份底稿都只是"那一期池子"的快照，
    # 后一期不含前一期跌出池子的票 —— evaluate.py 只能靠合并所有文件才凑齐历史，
    # 清理旧底稿就真的会丢数据（2026-11-11 实测：11-09 底稿比 11-06 底稿少了
    # sh600109/sh601878/sz301331 三只，回放 11-06 的名单从 155 掉到 152）。
    # 所以这里保留底稿里的全部代码，只做两件事：统一升序 + 截掉晚于 end 的 K 线。
    # 截断是必须的：回看旧日期（--date 早于底稿的截止日）时，底稿里的"未来"
    # K 线会让「末日≠end」把每只股票都判成未更新，require_codes 检查会直接报错。
    km = {}
    for c, v in base.items():
        if not v:
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
            got = _kline_fetch(ch, start, end, allow_empty=may_empty)
            if got is None:
                bad += 1
                sys.stderr.write(f"\n  第 {label} {n}/{len(chunks)} 批整体失败"
                                 f"（{len(ch)} 只），留给补取\n")
                if bad >= KLINE_BAD_LIMIT:
                    raise RuntimeError(
                        f"日K线连续 {bad} 批失败，接口疑似不可用，"
                        f"终止以免产出不可信名单")
            else:
                bad = 0
                _merge_bars(km, got)
                if not got and may_empty:
                    pass                                        # 整批停牌，正常
                elif len(got) >= len(ch):
                    pace = max(KLINE_PACE_MIN, pace * 0.7)      # 完整 → 慢慢提速
                else:
                    pace = min(KLINE_PACE_MAX, pace * 2 + 0.2)  # 仍缺 → 减速
                    sys.stderr.write(f"\n  {label} 第 {n} 批仍缺 "
                                     f"{len(ch) - len(got)}/{len(ch)} 只"
                                     f"（服务端限频），节流到 {pace:.1f}s\n")
            sys.stderr.write(f"\r  {label} {n}/{len(chunks)} 批  库内 {len(km)} 只")
            sys.stderr.flush()
            if KLINE_FLUSH_EVERY and n % KLINE_FLUSH_EVERY == 0:
                _flush(path, km, end)
            time.sleep(pace)
        sys.stderr.write("\n")
        if wi < len(work):      # 最后一批不做中途落盘，收尾还会写一次
            _flush(path, km, end)

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
            got = _kline_fetch(grp, beg, end, tries=2)
            _merge_bars(km, got or {})
            left = sum(1 for c in codes if not km.get(c))
            sys.stderr.write(f"\r    补取 {gi}/{len(groups)} 批，仍缺 {left} 只")
            sys.stderr.flush()
            time.sleep(0.3)
        _flush(path, km, end)
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
        _flush(path, km, end)

    # ② 「末日≠end」的票逐轮补取。**必须重试全部 stale，不能只重试 require_codes**：
    #    服务端限频时一批只回几只，非「当日在粗筛池」的票没有第二次机会，会被 ③
    #    当成停牌静默剔除 —— 等于悄悄漏股。2026-11-10 实测：4972 只里有 210 只明明
    #    在 11-09 有 K 线（逐只问过服务端，无一停牌），却因限频一直停在 11-06，
    #    导致当日名单少 2 只。
    #    补取按「最后一根 + 1 天」分组：每批只回 1~3 行，比整段 300 根便宜一个量级；
    #    用 _merge_bars 按日期合并（不整段替换），因此不存在「响应被截断而覆盖历史」的风险。
    #    硬性门禁仍然只有 require_codes：它们缺 end 日 K 一律报错。
    must = [c for c in codes if c in require_codes]
    for rnd in range(KLINE_STALE_ROUNDS):
        todo = [c for c in codes if km.get(c) and km[c][-1][0] != end]
        if not todo:
            break
        groups = {}
        for c in todo:
            nxt = (dt.date.fromisoformat(km[c][-1][0])
                   + dt.timedelta(days=1)).isoformat()
            groups.setdefault(nxt, []).append(c)
        sys.stderr.write(f"\n  校验：{len(todo)} 只 K 线未到 {end}，"
                         f"第 {rnd + 1} 轮补取 ...\n")
        for start, grp in sorted(groups.items()):
            for i in range(0, len(grp), KLINE_DELTA_BATCH):
                ch = grp[i:i + KLINE_DELTA_BATCH]
                _merge_bars(km, _kline_fetch(ch, start, end, tries=2,
                                             allow_empty=True) or {})
                time.sleep(0.3)
        _flush(path, km, end)
        still = sum(1 for c in todo if km.get(c) and km[c][-1][0] != end)
        sys.stderr.write(f"    本轮补上 {len(todo) - still} 只，仍缺 {still} 只\n")
        if still == len(todo):      # 毫无进展：再等也是一样，交给下面的判定
            break
        if rnd < KLINE_STALE_ROUNDS - 1:
            wait = min(KLINE_BACKOFF_MAX, KLINE_BACKOFF_BASE * (2 ** rnd))
            sys.stderr.write(f"    退避 {wait:.0f}s 后继续（限频窗口通常几十秒）\n")
            time.sleep(wait)
    left = [c for c in must if km.get(c) and km[c][-1][0] != end]
    if left:
        raise RuntimeError(
            f"K线未更新且重取失败：{left[:8]}"
            f"{'…' if len(left) > 8 else ''} 在 {end} 有成交，不能按停牌处理。"
            + (f"\n  当前 {dt.datetime.now():%Y-%m-%d %H:%M}，若 {end} 就是今天，"
               f"说明当日日线尚未收盘定稿，请 {MARKET_CLOSE:%H:%M} 之后重跑。"
               if not session_final(end) else "")
            + f"\n  已在 {KLINE_STALE_ROUNDS} 轮内补取（含逐批二分）仍不齐，"
              f"请隔几分钟重跑（已抓到的会增量复用）；否则加 --refresh 重下整段。")

    # ③ 收尾兜底：仍然末日≠end 的票，**用当日行情判定它到底有没有交易**。
    #    有成交 = 限频漏取，报错绝不产出可能漏股的名单；
    #    无成交 = 真停牌，如实按「无 end 数据」剔除。
    #    这一步是「宁可失败也不静默漏股」的最后一道门 —— 少了它，限频就会被
    #    伪装成停牌，名单少几只且毫无提示（2026-11-10 真实发生过）。
    stale = [c for c in codes if km.get(c) and km[c][-1][0] != end]
    if stale:
        verdict = _traded_on(stale, end)
        miss = [c for c in stale if verdict.get(c)]
        if miss:
            last = {c: km[c][-1][0] for c in miss}
            raise RuntimeError(
                f"K线漏取（非停牌）：{miss[:8]}{'…' if len(miss) > 8 else ''} "
                f"共 {len(miss)} 只在 {end} 有成交，但 K 线只到 "
                f"{sorted(set(last.values()))[-1]}。\n"
                f"  这些票若被当成停牌剔除，名单就会缺股，因此终止。"
                f"请隔几分钟重跑（已抓到的会增量复用），或加 --refresh。")
        unknown = [c for c in stale if c not in verdict]
        sys.stderr.write(
            f"  [警告] {len(stale)} 只 K 线末日≠{end}，经行情核对当日无成交"
            f"（停牌），按「无 {end} 数据」剔除\n")
        if unknown:
            sys.stderr.write(
                f"  [警告] 其中 {len(unknown)} 只没能从行情接口核对"
                f"（{unknown[:6]}{'…' if len(unknown) > 6 else ''}），"
                f"结论不完整；若名单看起来偏少，请隔几分钟重跑\n")

    for k in km:            # 兜底：保证全库升序（0.1 秒，换一次不变式检查）
        km[k].sort(key=lambda b: b[0])
    _flush(path, km, end)

    missing = [c for c in codes if not km.get(c)]
    critical_missing = sorted(set(missing) & require_codes)
    if critical_missing:
        raise RuntimeError(
            f"K线完全缺失：{critical_missing[:8]}"
            f"{'…' if len(critical_missing) > 8 else ''} 在 {end} 有成交，"
            f"不能按停牌处理。"
            + (f"\n  当前 {dt.datetime.now():%Y-%m-%d %H:%M}，若 {end} 就是今天，"
               f"说明当日日线尚未收盘定稿，请 {MARKET_CLOSE:%H:%M} 之后重跑。"
               if not session_final(end) else "")
            + "\n  否则请检查接口，或加 --refresh 重下整段。")
    return km, missing
