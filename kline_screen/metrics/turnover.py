"""20 日换手、池内分位与硬过滤。"""
from ..params import TURN_MIN_COVER, TURN_WINDOW


def volume_to_shares(code, volume):
    """接口返回的 volume → 「股」。

    ⚠️ 实测（2026-11）：科创板 sh688 的 volume 单位是「股」，
    其余板块是「手」（100 股）。amount ÷ (volume × close) ≈ 1.0 vs ≈100。
    任何「成交额/换手率」计算都必须过这一层，否则科创板会被高估 100 倍。
    """
    if volume is None:
        return None
    v = float(volume)
    return v if str(code).startswith("sh688") else v * 100


def bar_amount(code, bar):
    """单根 K 线的成交额（元）：有 amount 字段就直接用，否则 close×volume 估算。"""
    if len(bar) > 6 and bar[6] is not None:
        try:
            return float(bar[6])
        except (TypeError, ValueError):
            pass
    if len(bar) < 3 or bar[1] is None or bar[2] is None:
        return None
    try:
        return float(bar[1]) * volume_to_shares(code, bar[2])
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
    amts = []
    for d in cal20:
        b = by_day.get(d)
        if b is None:
            continue
        a = bar_amount(code, b)
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
                      "原因": "--turn-pct=0，换手只参与排序，不剔除"}, []
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
