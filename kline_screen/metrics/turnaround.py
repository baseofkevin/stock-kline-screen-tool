"""业绩转变三项标注（只标注，不裁决）。"""
from ..common.values import end_date, fnum, prev_quarter, pub_date
from ..datasources.finance import get_finance, get_finance_stmt
from ..params import (TURN_CL_REV_MIN, TURN_CL_YOY_MIN, TURN_GM_REQUIRE_YOY,
                      TURN_OCF_NP_MIN)
from .health import health_metrics
from .statements import _order_proxy


def turnaround_metrics(inc_rows, bal_rows, ratio_rows, day):
    """按基准日已披露的报表，计算「业绩转变」三个独立指标。

    ⚠️ 必须按 InfoPublDate ≤ day 过滤，避免未来函数。
    返回 None 表示三张表在基准日前都没有可用报告期（→「无数据」）。
    """
    def pit(rows):
        return [r for r in rows
                if pub_date(r.get("InfoPublDate")) and end_date(r)
                and pub_date(r.get("InfoPublDate")) <= day]

    inc = {end_date(r): r for r in pit(inc_rows)}
    bal = {end_date(r): r for r in pit(bal_rows)}
    rat = {end_date(r): r for r in pit(ratio_rows)}
    periods = set(inc) | set(bal) | set(rat)
    if not periods:
        return None

    # ①③ 同出 balancesheet，优先以它的最新报告期为准，避免 income 缓存滞后；
    # 缺 balancesheet 时退回 income，再退回 balance。
    if rat:
        t = max(rat)
    elif inc:
        t = max(inc)
    else:
        t = max(bal)

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

    # ---- ② 合同负债 / 在手订单增长 ----
    bdt = t if t in bal else max((d for d in bal if d <= t), default=None)
    cl_now = _order_proxy(bal[bdt]) if bdt else None
    cl_part = fnum((bal.get(bdt) or {}).get("ContractLiability")) if bdt else None
    ar_part = fnum((bal.get(bdt) or {}).get("AdvanceReceipts")) if bdt else None
    cl_prev = cl_growth = cl_ratio = None
    if bdt and cl_now is not None:
        prev_dt = f"{int(bdt[:4]) - 1}{bdt[4:]}"
        if prev_dt in bal:
            cl_prev = _order_proxy(bal[prev_dt])
        if cl_prev is not None and cl_prev > 0:
            cl_growth = (cl_now / cl_prev - 1) * 100
    rev_ttm = fnum((inc.get(t) or {}).get("OperatingRevenueTTM"))
    if rev_ttm is None:
        rev_ttm = fnum((rat.get(t) or {}).get("OperatingRevenueTTM"))
    if cl_now is not None and rev_ttm and rev_ttm > 0:
        cl_ratio = cl_now / rev_ttm * 100
    cl_applicable = cl_now is not None
    p2 = (cl_growth is not None and cl_growth >= TURN_CL_YOY_MIN
          and cl_ratio is not None and cl_ratio >= TURN_CL_REV_MIN)
    if not cl_applicable:
        why2 = "② 无合同负债/预收数据"
        p2_label = "无数据"
    elif cl_growth is None:
        why2 = "② 上年同期订单代理缺失或为0"
        p2_label = "无数据"
    elif cl_growth < TURN_CL_YOY_MIN:
        why2 = f"② 订单同比 {cl_growth:.1f}% < {TURN_CL_YOY_MIN:g}%"
        p2_label = "否"
    elif cl_ratio is None:
        why2 = "② 营业总收入TTM缺失"
        p2_label = "无数据"
    elif cl_ratio < TURN_CL_REV_MIN:
        why2 = f"② 订单/营收 {cl_ratio:.1f}% < {TURN_CL_REV_MIN:g}%"
        p2_label = "否"
    else:
        why2 = ""
        p2_label = "是"

    if not cl_applicable:
        cl_desc = "无合同负债/预收数据"
    else:
        cl_txt = (f"合同负债 {(cl_part or 0) / 1e8:.2f}亿"
                  f" + 预收款项 {(ar_part or 0) / 1e8:.2f}亿"
                  f" = {cl_now / 1e8:.2f}亿")
        growth_txt = ("同比 不可比（上年同季缺失或为0）"
                      if cl_growth is None else f"同比 {cl_growth:+.1f}%")
        ratio_txt = ("占营收TTM 无数据"
                     if cl_ratio is None else f"占营收TTM {cl_ratio:.1f}%")
        cl_desc = f"{cl_txt}；{growth_txt}；{ratio_txt}"

    # ---- ③ 经营现金流由负转正 / 现金流净利润 > 1 ----
    rt = rat.get(t) or {}
    ocf = fnum(rt.get("NetOperateCashFlowTTM"))
    np_ = fnum(rt.get("NPParentCompanyOwnersTTM"))
    if np_ is None:
        np_ = fnum((inc.get(t) or {}).get("NPParentCompanyOwnersTTM"))
    ocf_prev = fnum((rat.get(yoy_dt) or {}).get("NetOperateCashFlowTTM"))
    ratio = (ocf / np_ if ocf is not None and np_ is not None and np_ > 0
             else None)
    turn_pos = (ocf is not None and ocf > 0 and ocf_prev is not None
                and ocf_prev < 0)
    p3 = (ocf is not None and ocf > 0
          and (turn_pos or (np_ is not None and np_ <= 0)
               or (ratio is not None and ratio >= TURN_OCF_NP_MIN)))
    if ocf is None:
        why3 = "③ 经营现金流TTM缺失"
        p3_label = "无数据"
    elif ocf <= 0:
        why3 = f"③ 经营现金流TTM {ocf / 1e8:.1f}亿 ≤ 0"
        p3_label = "否"
    elif turn_pos:
        why3 = ""
        p3_label = "是"
    elif np_ is None:
        why3 = "③ 归母净利润TTM缺失"
        p3_label = "无数据"
    elif np_ > 0 and (ratio is None or ratio < TURN_OCF_NP_MIN):
        why3 = f"③ 现金流/净利润 {ratio:.2f} < {TURN_OCF_NP_MIN:g}"
        p3_label = "否"
    else:
        why3 = ""
        p3_label = "是"

    if ocf is None or np_ is None:
        ocf_np_text = ""
    elif np_ <= 0:
        ocf_np_text = "亏损/正" if ocf > 0 else "亏损/负"
    else:
        ocf_np_text = f"{ratio:.2f}" if ratio is not None else ""

    if ocf is None:
        ocf_desc = "经营现金流TTM 无数据"
    else:
        turn_txt = ("（由负转正）"
                    if ocf > 0 and ocf_prev is not None and ocf_prev < 0
                    else "")
        np_txt = ("净利润TTM 无数据" if np_ is None
                  else f"净利润TTM {np_ / 1e8:+.2f}亿")
        if np_ is None:
            ratio_txt = ""
        elif np_ <= 0:
            ratio_txt = "；亏损/正" if ocf > 0 else "；亏损/负"
        else:
            ratio_txt = (f"；OCF/NP {ratio:.2f}" if ratio is not None
                         else "；OCF/NP 无数据")
        ocf_desc = f"OCF_TTM {ocf / 1e8:+.2f}亿{turn_txt}；{np_txt}{ratio_txt}"

    health = health_metrics(inc_rows, bal_rows, ratio_rows, day)

    # 银行/保险/券商：没有毛利率、也没有合同负债语义 → 三项均「不适用」。
    # 行业类型用接口自带的 EnterpriseType 判断（balance / balancesheet 均返回，
    # income 没有）；不能靠「字段缺失」反推，否则会误标普通制造业。
    ent = next((r.get("EnterpriseType") for r in bal.values()
                if r.get("EnterpriseType")), None)
    is_financial = bool(ent) and ent != "一般企业"
    health_label = health["level"]
    health_desc = health["desc"]
    if is_financial:
        p1_label = p2_label = p3_label = "不适用"
        health_label = "不适用"
        health_desc = f"金融行业（{ent}），四项指标不适用"

    reasons = [x for x in (why1, why2, why3) if x]
    return {"reason": "；".join(reasons),
            "period": t,
            "gm": [g2, g3], "gm_text": gm_text, "gm_desc": gm_desc,
            "gm_yoy": round(gm_yoy, 2) if gm_yoy is not None else None,
            "rev_q": [r2, r3], "rev_ok": bool(rev_ok),
            "cl_period": bdt,
            "cl_growth": round(cl_growth, 2) if cl_growth is not None else None,
            "cl_ratio": round(cl_ratio, 2) if cl_ratio is not None else None,
            "cl_part": cl_part, "ar_part": ar_part, "cl_desc": cl_desc,
            "ocf_ttm": ocf, "np_ttm": np_, "ocf_prev": ocf_prev,
            "ocf_np": round(ratio, 3) if ratio is not None else None,
            "ocf_np_text": ocf_np_text, "ocf_desc": ocf_desc,
            "health": health, "health_label": health_label,
            "health_desc": health_desc,
            "enterprise_type": ent,
            "p1": bool(p1), "p2": bool(p2), "p3": bool(p3),
            "p1_label": p1_label, "p2_label": p2_label, "p3_label": p3_label}


def compute_turnaround(codes, day, refresh):
    """返回 ({code: metrics}, 失败代码列表)；全部失败返回 (None, codes)。"""
    if not codes:
        return {}, []
    inc, fail_inc = get_finance(codes, day, refresh)
    bal, fail_bal = get_finance_stmt(
        codes, day, refresh, "balance", "all", "finance_bal_{day}.json")
    rat, fail_rat = get_finance_stmt(
        codes, day, refresh, "balancesheet", "all", "finance_ratio_{day}.json")
    if not inc and not bal and not rat:
        return None, sorted(set(fail_inc + fail_bal + fail_rat))
    info = {}
    for c in codes:
        m = turnaround_metrics(inc.get(c) or [], bal.get(c) or [],
                               rat.get(c) or [], day)
        if m is not None:
            info[c] = m
    failed = sorted(set(fail_inc) | set(fail_bal) | set(fail_rat))
    return info, failed
