"""财务健康提示（只提示，不裁决）。"""
from ..common.values import end_date, fnum, pub_date
from ..params import (HEALTH_ANNUAL_YEARS, HEALTH_AR_GAP_WARN,
                      HEALTH_CASH_DEBT_DANGER, HEALTH_CASH_DEBT_WARN,
                      HEALTH_OCF_NP_WARN, HEALTH_QUICK_WARN)
from .statements import _stmt_annual, _stmt_latest


def health_metrics(inc_rows, bal_rows, ratio_rows, day):
    """四项财务健康提示：现金短债比、净现比、应收增速差、速动比率。

    数据口径（westock data_finance）：
      · 货币资金：`CashEquivalents`。接口没有单独的“受限资金”字段，
        因此这里只能按货币资金近似，不能真正做到“剔除受限”。
      · 短期有息负债：`ShortTermLoan` + `NonCurrentLiabilityIn1Year`。
      · 应收：`BillAccReceivable`（应收票据及应收账款）；营收：`OperatingRevenue`。
      · 净现比：年报 `NetOperateCashFlow` ÷ `NPParentCompanyOwners`。
      · 速动比率：(流动资产 − 存货) ÷ 流动负债。

    返回 dict，含 level（高危/关注/正常/数据不足）、desc、score 和原始值。
    只提示，不参与筛选、排序或裁决。
    """
    b = _stmt_latest(bal_rows, day)
    cash = fnum((b or {}).get("CashEquivalents"))
    st = fnum((b or {}).get("ShortTermLoan"))
    ncl = fnum((b or {}).get("NonCurrentLiabilityIn1Year"))
    if st is None and ncl is None:
        short_debt = None
    else:
        short_debt = (st or 0.0) + (ncl or 0.0)
    cash_debt = (cash / short_debt if cash is not None and short_debt
                 and short_debt > 0 else None)
    ca = fnum((b or {}).get("TotalCurrentAssets"))
    inv = fnum((b or {}).get("Inventories"))
    cliab = fnum((b or {}).get("TotalCurrentLiability"))
    quick = ((ca - inv) / cliab if ca is not None and inv is not None
             and cliab and cliab > 0 else None)

    # 净现比：最近 2 个年报
    net_years = []
    for r in _stmt_annual(ratio_rows, day, HEALTH_ANNUAL_YEARS):
        dt = end_date(r)
        np_ = fnum(r.get("NPParentCompanyOwners"))
        ocf = fnum(r.get("NetOperateCashFlow"))
        ratio = (ocf / np_ if np_ not in (None, 0) and ocf is not None
                 else None)
        bad = ratio is not None and ratio < HEALTH_OCF_NP_WARN
        net_years.append({"period": dt, "ocf": ocf, "np": np_,
                          "ratio": ratio, "bad": bool(bad)})
    net_bad = (len(net_years) >= HEALTH_ANNUAL_YEARS
               and all(x["bad"] for x in net_years[-HEALTH_ANNUAL_YEARS:]))

    # 应收增速差：最近 2 个年报，各自对比上一年
    bal_map = {end_date(r): r for r in bal_rows
               if pub_date(r.get("InfoPublDate"))
               and pub_date(r.get("InfoPublDate")) <= day}
    inc_map = {end_date(r): r for r in inc_rows
               if pub_date(r.get("InfoPublDate"))
               and pub_date(r.get("InfoPublDate")) <= day}
    annual_dates = sorted(d for d in inc_map
                          if d.endswith("-12-31") and d in bal_map)
    ar_gaps = []
    for dt in annual_dates[-HEALTH_ANNUAL_YEARS:]:
        prev = f"{int(dt[:4]) - 1}-12-31"
        if prev not in bal_map or prev not in inc_map:
            continue
        ar_now = fnum(bal_map[dt].get("BillAccReceivable"))
        ar_prev = fnum(bal_map[prev].get("BillAccReceivable"))
        rev_now = fnum(inc_map[dt].get("OperatingRevenue"))
        rev_prev = fnum(inc_map[prev].get("OperatingRevenue"))
        if None in (ar_now, ar_prev, rev_now, rev_prev):
            continue
        if ar_prev <= 0 or rev_prev <= 0:
            continue
        ar_gap = (ar_now / ar_prev - 1) * 100 - (rev_now / rev_prev - 1) * 100
        ar_gaps.append({"period": dt, "gap": ar_gap,
                        "ar_yoy": (ar_now / ar_prev - 1) * 100,
                        "rev_yoy": (rev_now / rev_prev - 1) * 100})
    ar_bad = (len(ar_gaps) >= HEALTH_ANNUAL_YEARS
              and all(x["gap"] > HEALTH_AR_GAP_WARN
                      for x in ar_gaps[-HEALTH_ANNUAL_YEARS:]))

    score = 0
    watch = False
    flags = []
    missing = []
    if cash_debt is not None:
        if cash_debt < HEALTH_CASH_DEBT_DANGER:
            score += 2
            flags.append(f"现金短债比 {cash_debt:.2f}（<{HEALTH_CASH_DEBT_DANGER:g} 高危）")
        elif cash_debt < HEALTH_CASH_DEBT_WARN:
            score += 1
            flags.append(f"现金短债比 {cash_debt:.2f}（<{HEALTH_CASH_DEBT_WARN:g} 紧张）")
    elif short_debt == 0:
        flags.append("无短期有息负债")
    else:
        missing.append("现金短债比")
    if net_bad:
        score += 2
        detail = "；".join(
            f"{x['period'][:4]} " + (f"{x['ratio']:.2f}" if x["ratio"] is not None
                                     else ("净利为0" if x["np"] == 0 else "—"))
            for x in net_years[-HEALTH_ANNUAL_YEARS:])
        flags.append(f"净现比连续{HEALTH_ANNUAL_YEARS}年<{HEALTH_OCF_NP_WARN:g}（{detail}）")
    elif len(net_years) < HEALTH_ANNUAL_YEARS:
        missing.append("净现比连续2年")
    elif net_years and net_years[-1]["ratio"] is not None and \
            net_years[-1]["ratio"] < HEALTH_OCF_NP_WARN:
        flags.append(f"净现比最新 {net_years[-1]['ratio']:.2f}（未连续2年）")
        watch = True
    if ar_bad:
        score += 2
        detail = "；".join(f"{x['period'][:4]} {x['gap']:+.1f}pct"
                           for x in ar_gaps[-HEALTH_ANNUAL_YEARS:])
        flags.append(f"应收增速差连续{HEALTH_ANNUAL_YEARS}年>+{HEALTH_AR_GAP_WARN:g}pct（{detail}）")
    elif len(ar_gaps) < HEALTH_ANNUAL_YEARS:
        missing.append("应收增速差连续2年")
    elif ar_gaps and ar_gaps[-1]["gap"] > HEALTH_AR_GAP_WARN:
        flags.append(f"应收增速差最新 {ar_gaps[-1]['gap']:+.1f}pct（未连续2年）")
        watch = True
    if quick is not None:
        if quick < HEALTH_QUICK_WARN:
            score += 1
            flags.append(f"速动比率 {quick:.2f}（<{HEALTH_QUICK_WARN:g}）")
    else:
        missing.append("速动比率")

    valid_count = sum(x is not None for x in (cash_debt, quick)) + \
        int(len(net_years) >= HEALTH_ANNUAL_YEARS) + \
        int(len(ar_gaps) >= HEALTH_ANNUAL_YEARS)
    if valid_count == 0:
        level = "数据不足"
    elif score >= 2:
        level = "高危"
    elif score == 1 or watch:
        level = "关注"
    else:
        level = "正常"
    desc_parts = []
    if flags:
        desc_parts.append("；".join(flags))
    elif level == "正常":
        desc_parts.append("四项未触发")
    if missing:
        desc_parts.append("缺：" + "、".join(missing))
    desc = "；".join(desc_parts)
    return {"level": level, "score": score, "desc": desc,
            "cash_debt": cash_debt, "quick": quick,
            "short_debt": short_debt, "cash": cash,
            "net_cash_years": net_years, "net_bad": bool(net_bad),
            "ar_gaps": ar_gaps, "ar_bad": bool(ar_bad),
            "missing": missing}
