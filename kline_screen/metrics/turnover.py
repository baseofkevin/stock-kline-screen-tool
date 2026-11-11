"""20 日换手、池内分位与硬过滤。"""
from ..params import TURN_MIN_COVER, TURN_WINDOW

#: volume 单位是「股」的代码前缀。科创板 sh688 与其 CDR sh689 都是「股」，
#: 其余板块（sh600/601/603/605、sz000/001/002/003/300/301/302、bj）是「手」。
#: 实测（2026-11）amount ÷ (volume × close)：sh688/sh689 ≈ 1.0，其余 ≈ 100。
STAR_PREFIXES = ("sh688", "sh689")


def unit_from_prefix(code):
    """按代码段推断 volume 的单位倍数：1 = 股，100 = 手。"""
    return 1.0 if str(code).startswith(STAR_PREFIXES) else 100.0


def infer_unit(code, bars):
    """用「有 amount 的 bar」反推该股 volume 的单位倍数；无法判定返回 None。

    为什么不能只靠前缀表：代码段会新增（sh689 CDR 就是漏掉过的例子），
    而 3 元组旧缓存与新抓的 7 元组混在一起时，同一只票的两种 bar 都得换算对。
    amount ÷ (close × volume) 落在 1 附近是「股」、落在 100 附近是「手」，
    用股票自己的成交数据标定，比维护前缀表稳。
    """
    for b in bars or ():
        if len(b) > 6 and b[6] and b[1] and b[2]:
            try:
                ratio = float(b[6]) / (float(b[1]) * float(b[2]))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if 0.5 <= ratio <= 2:
                return 1.0
            if 20 <= ratio <= 500:
                return 100.0
    return None


def volume_to_shares(code, volume, unit=None):
    """接口返回的 volume → 「股」。

    ⚠️ 实测（2026-11）：科创板 sh688 与科创 CDR sh689 的 volume 单位是「股」，
    其余板块是「手」（100 股）。amount ÷ (volume × close) ≈ 1.0 vs ≈100。
    任何「成交额/换手率」计算都必须过这一层，否则科创板会被高估 100 倍。
    unit 由调用方用 infer_unit() 标定后传入；缺省按代码段判断。
    """
    if volume is None:
        return None
    v = float(volume)
    return v * (unit if unit else unit_from_prefix(code))


def bar_amount(code, bar, unit=None):
    """单根 K 线的成交额（元）：有 amount 字段就直接用，否则 close×volume 估算。"""
    if len(bar) > 6 and bar[6] is not None:
        try:
            return float(bar[6])
        except (TypeError, ValueError):
            pass
    if len(bar) < 3 or bar[1] is None or bar[2] is None:
        return None
    try:
        return float(bar[1]) * volume_to_shares(code, bar[2], unit)
    except (TypeError, ValueError):
        return None


def turnover20(code, bars, cal20, cap_yuan):
    """最近 20 个交易日的日均成交额 ÷ 流通市值（%）。

    返回 (值, 覆盖天数)。覆盖 < TURN_MIN_COVER 天（停牌/缺数据）视为数据不足，
    返回 (None, n)，由调用方决定保留并标注，绝不因为缺数据静默剔除。
    """
    if not bars or not cap_yuan:
        return None, 0
    by_day = {b[0]: b for b in bars if b and b[0]}
    # 单位标定优先用该股自己的成交数据（混合缓存下也正确），无 amount 可比时退回代码段
    unit = infer_unit(code, bars) or unit_from_prefix(code)
    amts = []
    for d in cal20:
        b = by_day.get(d)
        if b is None:
            continue
        a = bar_amount(code, b, unit)
        if a is not None:
            amts.append(a)
    if len(amts) < TURN_MIN_COVER:
        return None, len(amts)
    return sum(amts) / len(amts) / cap_yuan * 100, len(amts)


def annotate_turnover(hits, pool_codes, km, cap_snapshot, cal20):
    """给入选股标注 20 日换手及其在当天粗筛池内的分位，返回 {code: 换手%}。

    分位 = 池内严格小于该值的比例（0 = 池内最冷，100 = 最热）。只标注、
    不剔除；数据不足（覆盖 <15 天）的记为 None 且分位为 None，绝不静默丢弃。
    """
    def cap_of(c):
        v = (cap_snapshot.get(c) or [None])[0]
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    pool_t = {}
    for c in pool_codes:
        t, _ = turnover20(c, km.get(c), cal20, cap_of(c))
        if t is not None:
            pool_t[c] = t
    vals = sorted(pool_t.values())
    m = len(vals)
    for x in hits:
        t, n = turnover20(x["code"], km.get(x["code"]), cal20, cap_of(x["code"]))
        x["turn20"] = round(t, 3) if t is not None else None
        x["turn20_days"] = n
        if t is not None and m:
            x["turn20_rank"] = round(sum(1 for v in vals if v < t) / m * 100, 1)
        else:
            x["turn20_rank"] = None
    return pool_t


def apply_turnover(hits, pool_codes, km, cap_snapshot, cal20,
                   pct_base, min_n, max_n):
    """按 20 日换手的池内分位处理名单，返回 (保留名单, 元信息, 剔除列表)。

    pct_base <= 0 或 >= 1：只排序标注、不剔除（默认，保持「上穿+放量」的本意）。
    0 < pct_base < 1：硬过滤，保留分位最低的 pct_base 段；保留数 < min_n
    放宽一档到 0.60，> max_n 收紧一档到 0.20（若 0.20 档会清空则保持原档）。
    池内可算换手的样本 <20 只时分位不稳定，退化为只标注。
    """
    pool_t = annotate_turnover(hits, pool_codes, km, cap_snapshot, cal20)
    m = len(pool_t)
    if not (0 < pct_base < 1):
        return hits, {"启用": False, "模式": "仅排序/标注", "窗口": TURN_WINDOW,
                      "池内样本": m,
                      "原因": f"--turn-pct={pct_base:g}，换手只参与排序，不剔除"}, []
    if m < 20:
        return hits, {"启用": False, "模式": "仅排序/标注", "窗口": TURN_WINDOW,
                      "池内样本": m,
                      "原因": f"池内可算换手的样本仅 {m} 只（<20），分位不稳定"}, []

    vals = sorted(pool_t.values())

    def cut(p):
        return vals[min(m - 1, int(m * p))]

    def keep_n(p):
        c = cut(p)
        return sum(1 for x in hits if x["turn20"] is not None and x["turn20"] <= c)

    pct, note = pct_base, ""
    n_base = keep_n(pct)
    if n_base < min_n and pct < 0.6:
        pct = 0.6
        note = f"按最低{int(pct_base*100)}%仅 {n_base} 只（<{min_n}），放宽到最低 60%"
    elif n_base > max_n and pct > 0.2 and keep_n(0.2) > 0:
        pct = 0.2
        note = f"按最低{int(pct_base*100)}%有 {n_base} 只（>{max_n}），收紧到最低 20%"
    elif n_base > max_n:
        note = f"按最低{int(pct_base*100)}%有 {n_base} 只（>{max_n}），但 20% 档会清空，保持最低 {int(pct*100)}%"

    c = cut(pct)
    keep, dropped = [], []
    for x in hits:
        if x["turn20"] is None:
            keep.append(x)
            continue
        if x["turn20"] <= c:
            keep.append(x)
        else:
            dropped.append((x["code"],
                            f"⑥ 20日换手分位 > 池内最低{int(pct*100)}%"
                            f"（{x['turn20']:.2f}% > {c:.2f}%）"))
    meta = {"启用": True, "模式": "硬过滤", "窗口": TURN_WINDOW, "池内样本": m,
            "保留档位": f"最低{int(pct*100)}%", "剔除线%": round(c, 3),
            "基数档位": f"最低{int(pct_base*100)}%", "自适应": note or "未触发",
            "剔除数": len(dropped)}
    return keep, meta, dropped
