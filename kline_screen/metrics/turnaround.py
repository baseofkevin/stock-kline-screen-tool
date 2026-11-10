"""业绩转变①毛利率环比改善标注（只标注，不裁决）。"""
from ..common.values import end_date, fnum, prev_quarter, pub_date
from ..datasources.finance import get_finance, get_finance_stmt
from ..params import TURN_GM_REQUIRE_YOY


def turnaround_metrics(inc_rows, ratio_rows, day):
    """按基准日已披露的报表，计算「①毛利率环比改善」。

    ⚠️ 必须按 InfoPublDate ≤ day 过滤，避免未来函数。
    返回 None 表示两张表在基准日前都没有可用报告期（→「无数据」）。

    口径（三项同时成立才算「是」）：
      ① 最近两个单季毛利率严格递增；
      ② 最新单季营收环比不为负；
      ③ 最新单季毛利率高于上年同季（TURN_GM_REQUIRE_YOY 开关，防季节性误判）。
    """
    def pit(rows):
        return [r for r in rows
                if pub_date(r.get("InfoPublDate")) and end_date(r)
                and pub_date(r.get("InfoPublDate")) <= day]

    inc = {end_date(r): r for r in pit(inc_rows)}
    rat = {end_date(r): r for r in pit(ratio_rows)}
    periods = set(inc) | set(rat)
    if not periods:
        return None

    # ① 同出 balancesheet，优先以它的最新报告期为准，避免 income 缓存滞后；
    # 缺 balancesheet 时退回 income。
    if rat:
        t = max(rat)
    else:
        t = max(inc)

    # ---- ① 最近 2 个单季毛利率递增 ----
    gm = {}
    for dt, r in rat.items():
        v = fnum(r.get("GrossIncomeRatio_Q"))
        if v is not None:
            gm[dt] = v
    # balancesheet 缺 GM_Q 时，用 income 累计营收/成本差分兜底
    for dt, r in inc.items():
        if dt in gm:
            continue
        rev = fnum(r.get("OperatingRevenue"))
        cost = fnum(r.get("OperatingCost"))
        if rev is None or cost is None or rev <= 0:
            continue
        m = int(dt[5:7])
        if m == 3:
            rq, cq = rev, cost
        else:
            pq = prev_quarter(dt)
            pr = inc.get(pq) if pq else None
            if not pr:
                continue
            prv = fnum(pr.get("OperatingRevenue"))
            pc = fnum(pr.get("OperatingCost"))
            if prv is None or pc is None:
                continue
            rq, cq = rev - prv, cost - pc
        if rq > 0:
            gm[dt] = (rq - cq) / rq * 100

    # 单季营收（用于「营收环比不为负」护栏）：优先 balancesheet 的 _Q 字段，
    # 缺失时用 income 的累计值差分。
    revq = {}
    for dt, r in rat.items():
        v = fnum(r.get("OperatingRevenue_Q"))
        if v is not None:
            revq[dt] = v
    for dt, r in inc.items():
        if dt in revq:
            continue
        rev = fnum(r.get("OperatingRevenue"))
        if rev is None:
            continue
        m = int(dt[5:7])
        if m == 3:
            rq = rev
        else:
            pq = prev_quarter(dt)
            pr = inc.get(pq) if pq else None
            if not pr:
                continue
            prv = fnum(pr.get("OperatingRevenue"))
            if prv is None:
                continue
            rq = rev - prv
        if rq is not None:
            revq[dt] = rq

    gm_applicable = bool(gm)
    q2 = prev_quarter(t)
    g2, g3 = gm.get(q2), gm.get(t)
    gm_ok = g2 is not None and g3 is not None and g2 < g3
    yoy_dt = f"{int(t[:4]) - 1}{t[4:]}"
    gm_yoy = gm.get(yoy_dt)
    gm_yoy_ok = gm_yoy is not None and g3 is not None and g3 > gm_yoy

    # 「营收环比不为负」：只检查最新一季 vs 上一季
    r2, r3 = revq.get(q2), revq.get(t)
    rev_ok = r2 is not None and r3 is not None and r3 >= r2
    if r2 is not None and r3 is not None:
        if r2 > 0:
            rev_txt = f"营收环比 {(r3 / r2 - 1) * 100:+.1f}%"
        else:
            rev_txt = "营收环比 不可比（基期为0）"
        if not rev_ok:
            rev_txt += "（最新一季营收环比为负）"
    else:
        rev_txt = "营收环比 数据不足"

    p1 = gm_ok and rev_ok and (not TURN_GM_REQUIRE_YOY or gm_yoy_ok)
    gm_text = "→".join("-" if v is None else f"{v:.2f}" for v in (g2, g3))
    if not gm_applicable:
        why1 = "① 无毛利率数据"
        gm_desc = "无毛利率数据"
        p1_label = "无数据"
    elif g2 is None or g3 is None:
        why1 = "① 近2季单季毛利率数据不足"
        gm_desc = f"{gm_text}；数据不足（需最近2个单季）"
        p1_label = "无数据"
    else:
        if not gm_ok:
            why1 = "① 近2季毛利率未环比改善"
            state = "环比未改善"
            p1_label = "否"
        elif not rev_ok:
            if r2 is None or r3 is None:
                why1 = "① 营收环比数据不足"
                state = "毛利率改善但营收环比数据不足"
                p1_label = "无数据"
            else:
                why1 = "① 毛利率改善但营收环比不达标"
                state = "毛利率改善但营收环比不达标"
                p1_label = "否"
        elif TURN_GM_REQUIRE_YOY and gm_yoy is None:
            why1 = "① 上年同季毛利率缺失，无法确认同比"
            state = "毛利率与营收达标，但同比不可比"
            p1_label = "无数据"
        elif TURN_GM_REQUIRE_YOY and not gm_yoy_ok:
            why1 = "① 最新单季毛利率未高于上年同季"
            state = "毛利率与营收达标，但未高于上年同季"
            p1_label = "否"
        else:
            why1 = ""
            state = "环比改善"
            p1_label = "是"
        if gm_yoy is None:
            yoy_txt = "同比 无数据"
        else:
            yoy_txt = (f"同比 {g3 - gm_yoy:+.2f}pct"
                       f"（上年同季 {gm_yoy:.2f}%）")
        gm_desc = f"{gm_text}（{state}）；{rev_txt}；{yoy_txt}"

    # 银行/保险/券商：没有毛利率语义 → 「不适用」。
    # 行业类型用接口自带的 EnterpriseType 判断（balancesheet 返回）；
    # 不能靠「字段缺失」反推，否则会误标普通制造业。
    ent = next((r.get("EnterpriseType") for r in rat.values()
                if r.get("EnterpriseType")), None)
    is_financial = bool(ent) and ent != "一般企业"
    if is_financial:
        p1_label = "不适用"

    return {"reason": why1,
            "period": t,
            "gm": [g2, g3], "gm_text": gm_text, "gm_desc": gm_desc,
            "gm_yoy": round(gm_yoy, 2) if gm_yoy is not None else None,
            "rev_q": [r2, r3], "rev_ok": bool(rev_ok),
            "enterprise_type": ent,
            "p1": bool(p1), "p1_label": p1_label}


def compute_turnaround(codes, day, refresh):
    """返回 ({code: metrics}, 失败代码列表)；全部失败返回 (None, codes)。"""
    if not codes:
        return {}, []
    inc, fail_inc = get_finance(codes, day, refresh)
    rat, fail_rat = get_finance_stmt(
        codes, day, refresh, "balancesheet", "all", "finance_ratio_{day}.json")
    if not inc and not rat:
        return None, sorted(set(fail_inc + fail_rat))
    info = {}
    for c in codes:
        m = turnaround_metrics(inc.get(c) or [], rat.get(c) or [], day)
        if m is not None:
            info[c] = m
    failed = sorted(set(fail_inc) | set(fail_rat))
    return info, failed
