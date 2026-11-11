"""数值、单位与日期小工具。"""


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def r2(v):
    """数值 → 保留 2 位小数；None / 非数字 → 空串（CSV 友好）。"""
    v = fnum(v)
    return "" if v is None else round(v, 2)


def cap_to_yuan(v):
    """⚠️ quote 的市值字段单位随 --date 变化：无 --date 为亿元，有 --date 为元。
    这里按数量级统一归一化为「元」。

    先过 fnum：接口给的是字符串（"40.26"），直接与 1e6 比大小会 TypeError。
    """
    v = fnum(v)
    if v is None:
        return None
    return v if v > 1e6 else v * 1e8


def pub_date(v):
    """InfoPublDate 形如 '2026-08-15 00:00:00 +0800 CST' → '2026-08-15'。"""
    return str(v)[:10] if v else None


def end_date(r):
    """报告期 EndDate → 'YYYY-MM-DD'。"""
    v = r.get("EndDate") if isinstance(r, dict) else None
    return str(v)[:10] if v else None


def prev_quarter(dt):
    """报告期 → 上一季末；非标准季末返回 None。"""
    try:
        y, m = int(dt[:4]), int(dt[5:7])
    except (TypeError, ValueError):
        return None
    if m == 3:
        return f"{y - 1}-12-31"
    if m == 6:
        return f"{y}-03-31"
    if m == 9:
        return f"{y}-06-30"
    if m == 12:
        return f"{y}-09-30"
    return None
